# Git-over-Nostr Host (GRASP) — implementation notes (P0–P4)

Native, self-contained **git-over-nostr host** for PosterChanAI: a smart-HTTP git server whose
pushes are authorized by **maintainer-signed Nostr events** (GRASP + NIP-34), backed by the built-in
relay's Postgres. No external services (no ngit-relay/ngit.dev), no HTTP passwords.

**OFF BY DEFAULT.** Everything is gated on the `git_server_enabled` setting (default `"false"`):
the supervisor spawns nothing and every `/api/git/*` route 404s until an admin turns it on. Shipping
it dormant is what makes a one-shot deploy safe. Gitea (`git.poster.place`) remains the deploy
backbone — `sync.sh` is untouched (that cutover is P5, deferred).

## Components (files added)

| File | Role |
|---|---|
| `git_host_main.py` | Child HTTP server (stdlib) on `127.0.0.1:<git_server_port=3053>`; execs `git http-backend` as CGI. Also enforces the **private-repo read gate**. Runs in its OWN process — all git work (upload/receive-pack, packing) is here, never on the app event loop. |
| `app/services/git_http_service.py` | Subprocess **supervisor** — a verbatim adaptation of the relay supervisor (singleton Popen, RLock, `_shutdown`, ~15s watchdog with crash-backoff, terminate→wait(4s)→kill). Gated on `git_server_enabled`. |
| `app/services/git_auth.py` | **Push-authorization core** (the security crux). Pure, import-light, unit-tested. Owns the decision function + the maintainer-ACL / 30618 Postgres reads + NIP-98 verify. |
| `app/services/git_host_service.py` | Repo provisioning: path mapping (traversal-proof), `git init --bare`, hook install, size/gc bounding, private-repo metadata. |
| `git_hooks/pre_receive.py` | The `pre-receive` hook (invoked by receive-pack). Fail-closed push validator; delegates the decision to `git_auth.decide_push_ref`. |
| `git_hooks/post_receive.py` | The `post-receive` hook. Publishes a normalized **30618 witness** (operator-signed, LOCAL relay only). Skips private repos. |
| `app/routers/git.py` | `/api/git/*` — host/list/announce/status. Hard-gated on `git_server_enabled`. |
| `scripts/grasp_selfhost.py` | P4 — host the `posterchanai` repo on the built-in host in PARALLEL with Gitea. Does NOT modify `sync.sh`. |

Edited: `app/schemas.py` (`git_server_*` settings), `app/database.py` (default `git_server_enabled=false`),
`app/main.py` (start/stop under the port-3051 guard + router registration),
`app/services/nostr_relay/thread.py` (ingest kinds + repo-scoped firehose acceptance),
`templates/admin/tabs/services.html` (Git Host settings card).

## Push authorization (P1) — fail-closed

A push to `refs/heads/<b>` is accepted **only** if the resulting ref→SHA is backed by a Nostr
**kind-30618** "repository state" event that:

1. is signed by an **authorized maintainer** — the maintainer ACL is read **only** from
   `30617:<owner-in-URL>:<id>` (owner ∪ that announcement's `maintainers` tag). A forged 30617 from
   a random pubkey is a *different* addressable coordinate and can never self-authorize;
2. has its **BIP-340 signature re-verified inside the hook** — never trusting the relay DB row's mere
   presence (defends a poisoned/compromised `events` row);
3. is the **newest by `created_at`** among maintainer-signed candidates (defeats stale-30618 replay);
4. names **exactly** the `<newsha>` git is writing for that ref (SHA-equality). `receive.fsckObjects`
   rejects malformed objects; the objects are quarantined and **discarded on any non-zero exit**.

Any error/ambiguity → reject (exit non-zero → git discards the quarantined objects). Deletes are
allowed only when the signed state also drops the ref; force-push (non-fast-forward) is allowed only
because a maintainer signed the new state, and only when `git_server_allow_force` is on.

**NIP-98 convenience path** (`git_server_nip98_push`, for automation/`sync.sh`): a maintainer-signed
`Authorization: Nostr <b64-27235>` header on the receive-pack POST authorizes the push; `post-receive`
derives/publishes the 30618. The header is re-verified (sig, method, `u` bound to this repo's
receive-pack path, created_at ±60s, signer ∈ maintainers).

The decision lives in `git_auth.decide_push_ref(...)` — a pure function unit-tested with crafted
events (see `tests/test_git_push_auth.py`).

## Private repos — READ gate (security-critical)

A repo can be marked **private** at create time (`private=true`; default configurable via
`git_server_default_private`). Private metadata is stored **on disk** (git config `pcai.private` /
`pcai.readers` + `grasp.json`) so the subprocess reads it without a DB hit.

- **Read (clone/pull) requires auth.** For a private repo, `git-upload-pack` (both the
  `GET info/refs?service=git-upload-pack` and the `POST git-upload-pack`) is gated in
  `git_host_main.py` **before** git-http-backend runs: require a valid **NIP-98** header whose signer
  is in the repo's ACCESS set = maintainers (owner ∪ 30617.maintainers, read from Postgres) ∪ the
  per-repo `readers` list. No/invalid/unlisted auth → **401**, and **nothing is served** (refs never
  leak — git-http-backend is never even spawned). Fail-closed: any error (DB unreachable, etc.) → 401.
- The NIP-98 header is re-verified (BIP-340 sig, `u` bound to this repo, created_at freshness). The
  method tag is **not** required for reads (a `git clone` reuses one static `http.extraHeader` across
  the info/refs GET + the upload-pack POST); the repo binding + freshness + access-set membership are
  the guard, over TLS. Read freshness window is 300s (push stays 60s — writes are higher-stakes).
- **Not announced.** A private repo publishes **no** public 30617/30618 and is excluded from the
  relay's repo-scoped collaboration acceptance — its title/content never reach the public relay.
  Discovery is via the admin-gated `/api/git/repos` listing only.
- **Collaboration stays local.** Private repos do not use the public Nostr issue/patch flow.
- Public repos are unchanged: anonymous clone as before.

Push auth for a private repo is the *same* maintainer-signed check; the read gate is the addition.

## Relay changes

- **Ingest allowlist** (`nostr_relay_ingest_kinds`): added `30618, 1617, 1621, 1622, 1623,
  1630–1633` alongside `30617` so the firehose can pull git collaboration events.
- **Repo-scoped acceptance** (P3, `thread.py` `_firehose_event`): patches/issues/replies/status +
  30618 are accepted from ANY author **only when they reference a repo THIS node hosts** (an `a` tag
  `30617:<owner>:<id>`, or a 30618's own coordinate, whose bare repo exists on disk **and is not
  private**). This keeps the WoT exemption from becoming an open spam firehose. 30617 announcements
  stay broadly public (Discover). Git kinds are not in `_PRUNABLE_KINDS`, so they're purge-exempt.

## Multi-node — git-server reverse proxy (like the Blossom storage proxy)

To run the git host on ONE node (e.g. `nas.lan`) and reach it from another (`server1`), set
`git_server_proxy_url` on the *other* node. Behavior mirrors the Blossom storage proxy exactly:

- **`git_server_proxy_url` set** ⇒ the node runs **no local git subprocess**. Its git front
  (`/git/<npub>/<id>.git/...`, `app/routers/git.py:git_smart_proxy` → `app/services/git_proxy.py`)
  is a **thin HTTP reverse-proxy** that forwards the smart-HTTP requests (`info/refs`,
  `git-upload-pack`, `git-receive-pack`) to `<git_server_proxy_url>/<npub>/<id>.git/...` on the
  hosting node — streaming, preserving the `Authorization`/NIP-98 header, `Content-Type`, and
  `Git-Protocol`. **All auth + repo storage + the pre-receive/post-receive hooks + the Postgres
  30617/30618 lookups stay on the hosting node** — the proxy is dumb and re-implements NO auth (it
  forwards the client's NIP-98 header and the hosting node authorizes; no server-to-server bypass).
  The request body is buffered so `Content-Length` is preserved for the host's CGI; the response
  (packfile) is streamed. Management endpoints (`/api/git/host|announce|repos`) refuse on a proxy
  node — provision on the hosting node.
- **`git_server_proxy_url` empty** ⇒ run the local subprocess as normal (gated on `git_server_enabled`).

Trust: `git_server_proxy_url` is admin-set config (same trust model as `storage_server_url`); an
http(s):// scheme is required. Mount matches the recommended nginx `location /git/`.

## Settings (all default-safe, relay-stored via settings_store `pcai:setting:<key>`)

`git_server_enabled` (**false**), `git_server_port` (3053), `git_server_bind` (127.0.0.1),
`git_server_public_base` (""), `git_server_allowlist` ("" ⇒ admins only), `git_server_repo_max_mb`
(512), `git_server_total_gb` (20), `git_server_allow_force` (true), `git_server_nip98_push` (true),
`git_server_default_private` (false), `git_server_proxy_url` ("" ⇒ local host).

Each key is (a) a typed field in `schemas.SettingsResponse`, (b) seeded in `database.py`
`default_settings`, (c) an Admin → Services input with `id`==`name`==key (so `static/js/admin.js`
hydrates + persists it generically). None are secret (no NIP-44 encryption needed).

## Deps

No new Python deps: stdlib `http.server` + `psycopg2` (already required) + `git`/`git-http-backend`
(ship together; confirmed at `/usr/libexec/git-core/git-http-backend`, git 2.54.0). The Dockerfile
already `apt-get install`s `git`. `./install.sh --git-host` verifies the prerequisites (no-op install).

## Verification

- `tests/test_git_push_auth.py` — 20 checks on `decide_push_ref` + NIP-98, incl. the 6 mandated push
  cases and 5 private read-gate cases (all crafted with really-signed events).
- `tests/test_git_host_serve.py` — supervisor gate (disabled ⇒ no spawn), public anonymous clone via
  git-http-backend, and the private read gate (401 anon / 200 allowlisted reader / 401 non-reader).
- `tests/test_git_push_e2e.py` — a real `git push` through git-http-backend → pre-receive → real
  Postgres: accept on matching maintainer-signed 30618, reject with no/mismatched state, and confirms
  a rejected push does not move the ref (objects discarded).
- `tests/test_git_proxy.py` — the reverse-proxy path: proxy-disabled ⇒ 404; public proxied `info/refs`
  is byte-identical to hitting the host directly; a private repo through the proxy 401s anonymously and
  200s with a forwarded reader NIP-98 header (auth stays on the host). Uses `httpx` (already a dep,
  via the storage proxy) — no new packages.

## Deferred

P5 (cut `sync.sh` over to the built-in host) — explicitly not done; Gitea stays the deploy backbone
and the break-glass path. PRs (1618/1619), Blossom pack offload, multi-host GRASP sync — later.
