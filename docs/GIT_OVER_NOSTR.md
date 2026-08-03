# Git-over-Nostr Host (GRASP) — implementation notes (P0–P4)

Native, self-contained **git-over-nostr host** for PosterChanAI: a smart-HTTP git server whose
pushes are authorized by **maintainer-signed Nostr events** (GRASP + NIP-34), backed by the built-in
relay's Postgres. No external services (no ngit-relay/ngit.dev), no HTTP passwords.

**OFF BY DEFAULT.** Everything is gated on the `git_server_enabled` setting (default `"false"`):
the supervisor spawns nothing and every `/api/git/*` route 404s until an admin turns it on. Shipping
it dormant is what makes a one-shot deploy safe. Gitea (`git.poster.place`) remains the deploy
backbone — `sync.sh` only **mirrors** each deploy onto the built-in host (P5, below); it never
deploys *from* it.

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
| `scripts/grasp_selfhost.py` | P4 — **provision + announce** (30617/30618) the `posterchanai` repo on the built-in host, in PARALLEL with Gitea. |
| `scripts/grasp_mirror.py` | P5 — **mirror commits**: publish a maintainer-signed 30618 for the new tip, then push it. Called by `sync.sh` on every deploy; runs on the hosting node, self-skips elsewhere. |

Edited: `app/schemas.py` (`git_server_*` settings), `app/database.py` (default `git_server_enabled=false`),
`app/main.py` (start/stop under the port-3051 guard + router registration),
`app/services/nostr_relay/thread.py` (ingest kinds + repo-scoped firehose acceptance),
`templates/admin/tabs/git_host.html` (Admin → Git settings card).

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

**Maintainer clone URLs.** A repo lives on disk under ONE owner (`<root>/<owner-hex>/<id>.git`), but
ngit derives a clone URL per key in the 30617 `maintainers` tag — so a two-maintainer repo is probed
at `<base>/<owner-npub>/<id>.git` **and** `<base>/<maintainer-npub>/<id>.git`, and the second one has
no directory. That printed `failed to list from https://…/<maintainer-npub>/<id>.git` on every push
even though the push to the owner's URL succeeded. `git_host_main._resolve_alias_owner` now maps a
maintainer's path segment back to the hosting owner (300s cache, `ghs.owners_hosting` + the same
`load_maintainers` ACL). It renames the URL and nothing else: the private-read gate, the write ACL and
the **owner-only** delete gate all still resolve against the canonical owner. Fail-closed — no DSN, no
candidate, or two hosted repos sharing the id all stay a 404.

PUSH authorization is immune to the URL spelling by construction, not by the alias being careful:
`pre_receive._owner_repo_from_gitdir` derives `<owner>` from **`GIT_DIR`'s parent directory on disk**
and re-validates it through `repo_dir()`, so the ACL coordinate is always `30617:<hosting-owner>:<id>`
however the request was addressed. The NIP-98 push needle (`<id>.git/git-receive-pack`) carries no
owner segment, so a maintainer-signed header works through either URL while cross-repo replay stays
blocked.

The alias lookup runs BEFORE any auth gate, so its cache is keyed on the **repo**, never on the
caller's npub — keyed on the caller, an anonymous client could mint a Postgres connection per made-up
npub and evict the real entries on the way. Per repo the ACL is read once per 300s TTL; an unknown
npub is a dict miss. A Postgres blip caches an empty map for that TTL, so the cosmetic `failed to
list` warning can return for up to 5 minutes after one — degrading to the old behaviour, never worse.
Private repos never alias at all: `create` never announces them, so there is no 30617 to read.

## Browse + write API (what the web UI renders from)

Beyond the three smart-HTTP endpoints, `git_host_main.py` serves a small read API — all **read-gated
exactly like a clone** (private repos need NIP-98) — plus **one** write route:

| Route | Returns |
|---|---|
| `GET …/raw/<ref>/<path>` | one file's bytes (2 MB cap — it exists to render a README) |
| `GET …/download/<ref>/<path>` | the same bytes as an attachment, streamed from `git cat-file`, 64 MB cap |
| `GET …/tree/<ref>[/<dir>]` | directory listing + each entry's last commit |
| `GET …/log/<ref>[/<path>]?limit=N` | commit history |
| `GET …/refs` | branches + tags + the default branch (the UI's ref switcher) |
| `GET …/commit/<sha>` | one commit: metadata, per-file `+/-` stats and patch (bounded, `truncated` flag) |
| `POST …/edit` | **write**: commit one file add/change/delete |

Every route also accepts **`?ref=`**, which overrides the ref in the path — a branch named
`feature/x` cannot be expressed as a single path segment. Refs are validated by `_valid_ref()`
(must start alphanumeric, so a ref can never be read as a `git` option; no `..`, `@{`, `//`).

`git diff-tree` runs with `--root -m --first-parent` (so the first commit and merges both show a
diff) and deliberately **without `-M`**: rename detection makes `--numstat` emit a combined
`dir/{a => b}` field that never matches the `diff --git` path, so stats and patch would key
differently and one rename would render as three rows.

### The web editor's write path (`POST …/edit`)

Authorization is the SAME primitive as a push, so the editor can't exceed `git push`:

1. A **NIP-98** (kind-27235) header, signature re-verified here, bound to `<id>.git/edit` (a
   read-scoped or other-repo header is refused), method-matched, fresh, and signed by a key in the
   **maintainer ACL** read from `30617:<owner>:<id>` — identical ACL code (`git_auth.load_maintainers`)
   to `pre-receive`. No DSN ⇒ owner only (fail-closed).
2. The commit is built with plumbing against a **temporary index** (`GIT_INDEX_FILE` + `read-tree` →
   `update-index --index-info` → `write-tree` → `commit-tree`), never a work tree — a bare repo has
   none. Staging uses `--index-info` for BOTH add and delete because `--force-remove`/`--cacheinfo`
   demand a work tree.
3. `base` (the sha the editor opened) is a **compare-and-swap**: mismatch ⇒ **409**, and
   `update-ref <ref> <new> <old>` re-checks at the git level, so a concurrent push can't be clobbered.
4. Committing to a tag is refused; symlinks are refused; the author is recorded as `<npub>@nostr`.
5. `receive-pack` hooks do NOT run for this path, so it publishes the operator **30618 witness**
   itself (`git_host_service.publish_state_witness`, shared with `post-receive`) and returns
   `state_tags_30618` for the CLIENT to sign+publish — the maintainer's own 30618 stays the
   push-authorization authority, so a web commit leaves the repo as Nostr-attested as a push does.

App-side proxies (`/client/git/{tree,log,refs,commit,blob,download,edit}` in `app/routers/client.py`)
just forward to the host — `/edit` passes the caller's `Authorization` header through and holds **no**
authority of its own.

## Chunked request bodies (the "400 Bad request syntax" push bug)

git switches to `Transfer-Encoding: chunked` as soon as a pack exceeds `http.postBuffer` (1 MB by
default), so this is the normal shape of a **first full push**. The host used to read `Content-Length`
only: the body was never consumed, the leftover chunk framing was parsed as the next request line, and
the push died with `400 Bad request syntax` (worked around per-node with
`git config http.postBuffer 524288000`).

`_read_chunked_body()` now de-frames the body (chunk sizes, extensions, trailers — consuming it
*exactly*, which is what keeps the connection parseable) into a spool file on the repo volume, then
hands `git-http-backend` a real `CONTENT_LENGTH`. Receive-pack needs the whole pack anyway, so
spooling costs nothing in practice; `_MAX_BODY` (2 GiB) is enforced while de-framing.

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
- **Plain git reads private repos via a Basic envelope.** The read gate (and ONLY the read gate —
  `allow_basic=True` is passed nowhere else) also accepts the same base64 NIP-98 event as the
  **password half of HTTP Basic**, so a client that can only do username/password still presents a
  real signed token. Every check is unchanged: BIP-340 re-verify, `u` bound to this repo, freshness,
  ACL membership. It is a second envelope for one signed token, not a second way to authenticate —
  an ordinary password still fails (covered in `tests/test_git_push_auth.py`). The 401 advertises
  both `Nostr` and `Basic`, since a client only attempts a scheme the server offers.
  Client side: `scripts/git-credential-nostr` mints a **fresh** token per request (git invokes a
  helper on every request, so nothing goes stale inside the 300s read window) and **requires
  `credential.useHttpPath=true`** — without the path it cannot bind the token to a repo, so it emits
  nothing and fails closed. Setup:

      git config --global 'credential.https://poster.place.helper' \
        '!/path/to/venv/bin/python /path/to/scripts/git-credential-nostr'
      git config --global 'credential.https://poster.place.useHttpPath' true

- **PUSH over plain https is NOT supported — use a `nostr://` remote.** The Basic envelope above is
  the READ gate only (`allow_basic` is never passed on the push path), and a plain `git push` to an
  https clone URL therefore dies as `! [remote rejected] … (pre-receive hook declined)`. The cause is
  structural, not a missing feature: `decide_push_ref` wants either an `Authorization: Nostr` header,
  which nothing in the git client stack emits, or a maintainer-signed 30618 pinning the exact sha,
  which is what the `nostr://` remote publishes for itself. A credential helper cannot bridge the gap
  — **git hands a helper only the repo path** (`path=git/<npub>/<id>.git`, identical for fetch and
  push, measured), so it cannot mint a token bound to `<id>.git/git-receive-pack` with `method=POST`.
  Closing this properly means the host challenging (401) on receive-pack and naming the URL+method to
  sign in the `WWW-Authenticate` realm, which git does relay to helpers as `wwwauth[]`. Until then:
  **clone/fetch over https, push over `nostr://`.**

  Corollary, and it bites: an https push via a hand-made NIP-98 header authorizes by route 0 and
  publishes **no** 30618, so the Nostr-side state only moves if `post-receive`'s operator witness
  lands. While that reports `witness 30618 not published`, such a push leaves the 30618 stale — git
  reads stay correct (they read the real repo) but a `nostr://` fetch reports the OLD sha and
  resurrects deleted branches. Pushing the branch over `nostr://` republishes the state and repairs it.

### ngit + private repos: two things had to be fixed

Stock ngit 2.6.3 cannot read a private repo here, and it fails **silently** — unable to list refs it
reports `Everything up-to-date` and pushes nothing. Two independent causes, both measured:

1. **ngit only tries the unauthenticated protocol against a GRASP server.**
   `get_read_protocols_to_try`/`get_write_protocols_to_try` return a one-element list for a GRASP
   server, so `dont_authenticate` is set and the credentials callback is never installed — which is
   why a credential helper is never invoked (it isn't that ngit lacks the ability: it uses
   `auth_git2`, which does run helpers; it just never reaches that path). **Patched locally** so the
   GRASP branch falls back to the authenticated protocol; `~/.local/bin/ngit.stock-2.6.3` and
   `git-remote-nostr.stock-2.6.3` are the untouched originals. Worth upstreaming.
2. **This host looked like a GRASP server without being one.** ngit classifies by URL shape
   (`is_grasp_server_clone_url`: any `https://host/<npub>/<repo>.git`) and then derives the relay by
   truncating at the npub — so `https://poster.place/git/<npub>/<x>.git` implies a relay at
   `wss://poster.place/git`. Nothing answered there, so pushes died with `state event failed to
   reach any git server relay`. Fixed in the router's nginx: `location = /git` (exact match, so the
   longer smart-HTTP paths still reach the app) proxies to a relay on `:3052`.

**That endpoint must be the HOSTING node's relay.** `pre-receive` reads its own node's relay
Postgres for the 30617 maintainer ACL and the 30618 authorizing the push, and server1/nas run
separate relays with separate event stores — so `location = /git` proxies to **nas**, not server1.
Pointing it at server1's relay silently appears to work only if the client also publishes to nas by
some other route; on its own it rejects every push.

With both in place a private repo works over **HTTPS, using only public URLs** — no SSH, no LAN
hostnames (a `ws://nas.lan:3052` in the relay list works from the LAN and strands anyone off it):

    ngit init --name <id> --clone https://poster.place/git/<npub>/<id>.git \
              --relay wss://relay.poster.place --relay wss://poster.place/git

`/opt/admintools` and `/opt/gentoo-installer` are set up this way on both server1 and router.lan:
they clone from a bare `nostr://` URL (`fetch: succeeded over https`), pushes are authorized by the
signed 30618 reaching nas through `wss://poster.place/git`, and anonymous `info/refs` still 401s.

- **Announcing at all makes the identifier public.** ngit resolves `nostr://<npub>/<id>` *from* the
  30617, so an unannounced repo has nothing to resolve. `relay.poster.place` serves anonymous reads,
  so the repo **id is public even when the code is not** — announce private repos with no
  description.
- **Push relays matter.** `pre-receive` reads the **hosting node's** relay Postgres, and the two
  nodes have separate event stores, so a 30618 published only to `relay.poster.place` (server1) is
  invisible to nas. List the hosting node's relay (`ws://nas.lan:3052`) in the repo's relays or
  pushes fail to authorize.
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

**The hosting node must bind a reachable interface.** The git host defaults to `git_server_bind =
127.0.0.1`, which a peer can't reach. On the hosting node (`nas.lan`) set **`git_server_bind =
0.0.0.0`** (or its LAN IP) and restart so the subprocess re-binds `:3053`, then set the *proxy* node's
`git_server_proxy_url = http://nas.lan:3053`. (LAN only — keep `:3053` firewalled off the public
internet; the public edge reaches it through nginx `/git/`, never directly.)

**nginx per role.** `location /git/` on the **hosting** node proxies to the git host
(`proxy_pass http://127.0.0.1:3053;`); on the **proxy** node it proxies to the *app*
(`proxy_pass http://127.0.0.1:3051;`), whose `git_smart_proxy` forwards to the host. See
[NGINX.md](NGINX.md).

Trust: `git_server_proxy_url` is admin-set config (same trust model as `storage_server_url`); an
http(s):// scheme is required. Mount matches the recommended nginx `location /git/`.

**Concrete example — this deployment (`nas.lan` hosts, `server1` proxies):**

| node | `git_server_enabled` | `git_server_bind` | `git_server_proxy_url` | role |
|------|------|------|------|------|
| `nas.lan` | true | `0.0.0.0` | *(empty)* | hosts the repos on `:3053` |
| `server1` | — | *(default)* | `http://nas.lan:3053` | proxies `/git/` → nas |

## Settings (default-safe; Admin → Git)

`git_server_enabled` (**false**), `git_server_port` (3053), `git_server_bind` (127.0.0.1),
`git_server_public_base` (""), `git_server_allowlist` ("" ⇒ admins only), `git_server_repo_max_mb`
(512), `git_server_total_gb` (20), `git_server_allow_force` (true), `git_server_nip98_push` (true),
`git_server_default_private` (false), `git_server_proxy_url` ("" ⇒ local host).

**Scope.** The **policy** keys (`git_server_allowlist`, `git_server_public_base`,
`git_server_repo_max_mb`, `git_server_total_gb`, `git_server_allow_force`, `git_server_nip98_push`,
`git_server_default_private`) are **shareable/global** — relay-stored via settings_store
`pcai:setting:<key>`, so the same repos + rules apply on every node. The four **topology** keys —
`git_server_enabled`, `git_server_bind`, `git_server_port`, `git_server_proxy_url` — are **per-node**
(local-only, in each node's `local_settings.json`, listed in `settings_store._PLUMBING_KEYS`), exactly
like `nostr_relay_enabled/bind/port`. That's what lets one node host (`enabled = true`, `proxy_url`
empty, `bind = 0.0.0.0`) while another proxies to it (`enabled = false`, `proxy_url =
http://host:3053`) without the shared relay doc leaking one node's enable/proxy_url onto every node.
Editing a topology key in a node's Admin persists it to *that* node's JSON and **reconciles the running
git-host subprocess in place** (no full restart — see `app/routers/admin.py`); editing a global key
writes through to the relay for all nodes.

Each key is (a) a typed field in `schemas.SettingsResponse`, (b) seeded in `database.py`
`default_settings`, (c) an Admin → Git input with `id`==`name`==key (so `static/js/admin.js`
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
- `tests/test_git_host_browse_edit.py` — 47 checks on the browse API + web editor + chunked push:
  refs/tree/log/commit shapes (incl. root + merge commits), `?ref=` with a slashed branch, download
  headers, hostile refs/paths refused, all four `/edit` authorization refusals, commit/delete/exec-bit/
  new-file/CAS-409 behaviour, and a real 4 MB `git push` with `http.postBuffer=16k` (chunked) that
  lands the right sha and leaves the repo fsck-clean.
- `tests/test_git_proxy.py` — the reverse-proxy path: proxy-disabled ⇒ 404; public proxied `info/refs`
  is byte-identical to hitting the host directly; a private repo through the proxy 401s anonymously and
  200s with a forwarded reader NIP-98 header (auth stays on the host). Uses `httpx` (already a dep,
  via the storage proxy) — no new packages.

## P5 — `sync.sh` mirrors every deploy to the nostr repo

`scripts/grasp_mirror.py` keeps the hosted repo's commits in sync with `origin` (it had drifted 8
commits behind before this existed). Gitea (`origin`) is still the **deploy backbone** and the
break-glass path — the GRASP repo is a **mirror**, like the `github` remote, not a cutover.

- **Where it runs: the HOSTING node.** Push auth is a maintainer *signature*, not a connection: only
  a maintainer of `30617:<owner>:<id>` can move a ref, and `pre-receive` reads the **hosting node's**
  relay Postgres (`GRASP_PG_DSN`) — a 30618 published to another node's relay isn't seen. On the
  hosting node the operator key IS the repo owner, hence always a maintainer. A proxy node
  (`git_server_proxy_url` set), a node with the host off, or one with no operator key **skips with a
  message and exit 0**. `sync.sh` therefore invokes it on **both** server1 and nas: whichever hosts
  does the work, the other prints `skipping` — no topology hardcoded in the deploy script.
- **Two proofs per push (either suffices).** It publishes a maintainer-signed **30618** naming the
  new tip for the pushed ref (carrying the repo's other refs forward unchanged) to the local relay,
  then pushes over smart-HTTP with a **NIP-98** header (`git_server_nip98_push`). The 30618 is the
  canonical GRASP path and keeps the relay's advertised state correct; the header means a slow relay
  write can't wedge a deploy. `post-receive` then republishes the operator witness as usual.
- **Idempotent + best-effort.** If the hosted ref already equals the local tip it prints
  `already current` and does nothing; any failure prints `[grasp] …` and the deploy carries on
  (`sync.sh` wraps it in `|| echo WARN`). `--dry-run` reports what would move.
- Provisioning + the 30617 announcement stay in `scripts/grasp_selfhost.py`; the mirror refuses to
  create a repo (that would overwrite a configured announcement with defaults) and points there.

Verified on this deployment: `nas.lan` mirrored `master` `61821659…` → `c0887701…`, the re-run
no-op'd, and the repo clones anonymously from `https://poster.place/git/<npub>/posterchanai.git`.

## Deferred

PRs (1618/1619), Blossom pack offload, multi-host GRASP sync — later. Mirroring from a *non*-hosting
node would need that node's npub added to the repo's 30617 `maintainers` (then NIP-98 alone
authorizes it); not done, since the hosting node already has the key.
