# Media Center

Media Center scans server video and audio folders into a private library, sorted naturally
by folder and filename (Season 2 before Season 10). Open **Media Center** in the sidebar or
desktop launcher. A Nostr-signed-in administrator can add a folder; the owning administrator
can rescan and grant or revoke access using npubs or hex public keys. All configuration
requires a current admin role, including for an owner whose admin role was removed.
Enable **Additional permissions → Media Center** on each viewer’s account, then share
the library with that user. Administrators have access without this checkbox.
Authorized ordinary users can browse and play, but cannot scan, change sharing or limits,
or see server folder paths and sharing controls. Shared users sign in on the
hosting Posterchan instance. No social messages or invitations are sent automatically.

**My libraries** lists libraries you own; **Shared with me** lists libraries explicitly
shared with your Nostr account. Viewers with only shared libraries open that tab by default.
Removing sharing access removes the library from that tab and revokes playback access.
Opening a library shows its server folders immediately, without waiting for media probing.
Select folder cards and use the breadcrumb buttons to go back; search finds scanned titles
throughout the library. Scanning runs in the background and playable titles appear as they
are discovered. Refreshing scan results preserves the current folder and active player.
Folder cards prefer the folder's `folder.png`, with title artwork as a fallback. Rescan
creates missing `folder.png` files from a video frame, including ancestor folders, with
one bounded decode at a time. Existing artwork is never replaced. The service needs write
access to create artwork; read-only folders remain playable and keep existing artwork.
Artwork loads only
near the viewport, with two requests at a time. A `.ignore` marker excludes its folder and
all descendants from scanning and browsing, including previously indexed titles.
Long scans save encrypted catalog checkpoints after the first five seconds and then at
most every twenty seconds. After a restart, saved titles remain available; an interrupted
scan can be resumed with **Rescan**, reusing metadata for unchanged files.

The source files stay on disk. Catalog pages, library ownership, sharing lists and limits
are NIP-44-encrypted, operator-signed kind-30078 events under `pcai:media-center:` in the
local relay. This namespace is excluded from public federation, disaster-recovery config
fan-out, and private mirror fan-out. Other Nostr clients do not independently discover or
decrypt this server-mediated library. Back up the local relay and operator storage key.

## NAS proxy and persistence

On the public node, open **Admin → Storage → Media Center Server URL** and set the
NAS origin, for example `http://nas.lan:3051` (use the NAS's actual Posterchan port).
Set the same **Node-to-node shared secret** on both servers. Media Center requires
this secret and does not accept legacy header-only peer authentication.

On `nas.lan`, leave **Media Center Server URL** empty. Run Posterchan with its local
relay, FFmpeg, and access to the media mount. The public node streams responses through
without buffering complete files, while the NAS performs scans, stores catalog events,
checks sharing permissions, and enforces one set of bandwidth/concurrency limits across
proxied and direct viewers. A loop or unavailable NAS produces an error; there is no
fallback to a different node's local library.

`media_center_server_url` persists in the node's local settings file and loads at
startup. It is included in the admin settings schema so saving, clearing and reopening
the field work. It never hydrates onto a different node from relay settings. Library
folders, encoder choice, sharing lists, catalog pages and bandwidth/resource limits
persist as encrypted events on the NAS and read back after a cold restart. Environment
variables for allowed roots, the cache directory and VA-API device belong in the NAS's
persistent service configuration.

## Server setup

Install `ffmpeg` and `ffprobe`, and give the Posterchan service read access to the media
folder. The default allowed root is `$POSTERCHANAI_DATA/media`, or
`/var/lib/posterchanai/media` when that variable is unset. To allow existing media mounts,
set this environment variable in the service configuration and restart it:

```ini
POSTERCHANAI_MEDIA_ROOTS=/srv/media:/mnt/movies
POSTERCHANAI_MEDIA_CACHE=/tmp/posterchan-media-center
POSTERCHANAI_MEDIA_VAAPI_DEVICE=/dev/dri/renderD128
```

Only folders inside allowed roots can be added, and symbolic-link media entries are
excluded. The cache directory must be writable by the service; it defaults to
`/tmp/posterchan-media-center` with owner-only directory permissions. Overrides must also
remain under `/tmp`. To avoid SSD writes, mount `/tmp` as tmpfs on the NAS; the directory
name alone does not imply RAM-backed storage. Keep the cache budget below the available
tmpfs capacity, leaving room for concurrent temporary segments. Cache segments are
ordinary playable bytes protected by the filesystem and API access checks, not encrypted
media blobs. Source media is not encrypted or uploaded by scanning.

Select automatic, CPU, NVIDIA, AMD, or VA-API transcoding when adding a library. CPU uses
libx264; NVIDIA uses NVENC; AMD tries VA-API then AMF. Automatic tries the hardware paths
before CPU. FFmpeg must include the encoder and the service must have access to the
corresponding drivers/device. Failed hardware attempts are suppressed for five minutes,
with CPU fallback. See [FFmpeg codec documentation](https://ffmpeg.org/ffmpeg-codecs.html).

### Installer and Docker

The bare-metal installer creates `media-center.env` beside `install.sh` only when
absent. Its generated systemd service reads that file on startup, preserving your
roots and device settings across installer updates. Edit it and restart the service.
FFmpeg/ffprobe are operating-system dependencies; Media Center uses the existing
Python dependencies in both `requirements.txt` and `requirements-nostr.txt`.

Docker includes FFmpeg, CPU H.264 and Mesa VA-API drivers. To mount a NAS folder
read-only and use NVIDIA on the NAS:

```sh
POSTERCHANAI_MEDIA_PATH=/mnt/media docker compose \
  -f docker-compose.yml -f docker-compose.media.yml --profile cuda up -d --build
```

To let Rescan create missing folder artwork in this mount, also set
`POSTERCHANAI_MEDIA_READ_ONLY=false`. The default mount remains read-only.

The host needs NVIDIA drivers and NVIDIA Container Toolkit. The image enables the
`video` driver capability for NVENC. Use `--profile cpu` or `--profile nostr` for
CPU transcoding; AMD/Intel profiles expose `/dev/dri`. All application profiles mount
`/tmp/posterchan-media-center` as a 2560 MB tmpfs. Set
`POSTERCHANAI_MEDIA_TMPFS_SIZE` to change its capacity; keep the Media Center cache
limit below that capacity with room for concurrent temporary segments. The source
mount must already exist. Catalogs remain in the persistent local relay.

## Bandwidth and resource controls

Administrators open **Bandwidth and resource limits** inside Media Center:

| Setting | Default | Meaning |
| --- | ---: | --- |
| Total bandwidth | 20,000 kbps | Combined video response-byte budget for all viewers |
| Bandwidth per user | 1,600 kbps (200 KB/s) | Combined budget for one Nostr identity, including multiple tabs |
| Simultaneous streams | 8 | Maximum active playback sessions; idle slots expire after 90 seconds |
| Concurrent transcodes | 2 | Maximum simultaneous segment-generation jobs |
| Segment cache | 2,048 MB | Disk budget, with least-recently-used segment eviction |

Limits are saved as local encrypted Nostr events. Use one ASGI worker for this media
host: admission, bandwidth pacing and job counters are process-local. Multiple server
processes require a shared limiter before these can be treated as host-wide limits.
The per-user limit must not exceed the total limit. These are media payload rates, not
NIC-level limits on all Posterchan traffic or TLS overhead. Pacing uses 16 KiB chunks,
so a small initial burst is possible. A lower setting takes effect on subsequent segment
requests; already-running responses retain their current rate.

The default 200 KB/s per viewer permits the 360p and 480p profiles. Raise this limit
to enable 720p or 1080p. The independent default server cap remains 20 Mbps. Saved
custom limits take precedence over defaults after restart.

The library uses Webxdc's responsive cover-card grid, with two columns on phones,
larger controls on large screens, search, keyboard focus indicators, and a Full screen
button. Press F while focused in the player to toggle full screen; native video controls
remain available. Local artwork can be a matching media basename with `.jpg`, `.png`
or `.webp`, or `poster.jpg`, `cover.jpg`, `folder.jpg`, or `cover.png` in its folder.
Only visible covers are fetched; the server strips and downsizes them and caches up to
128 thumbnails. There is no poster scraping or background video-frame extraction.

The HLS player automatically selects among 360p, 480p, 720p and 1080p, and offers manual
quality selection. Profiles exceeding the configured user limit are omitted and rejected
by the server. When the combined server budget is insufficient, streams must adapt down
or may buffer; set the simultaneous-stream limit to match the available bandwidth.

Only requested six-second segments are encoded. Viewers share cached segments across
sessions, and duplicate requests reuse the same work. Stopping playback stops requesting
segments; an in-progress segment may finish. Unchanged files reuse their probe metadata
on rescan, unchanged catalog pages reuse encrypted events, and immutable catalog pages
are cached in memory. Scans run in the background, one at a time, so a large folder does
not hold a Cloudflare HTTP request open or block playback/sharing changes. A new manifest
is committed only after every page is saved. Previous changed pages remain in the local
relay as snapshots; they are not copied upstream. Restarting during a scan leaves the
previous committed catalog intact; rescan to complete the interrupted work.

Current bounds: 100 libraries and 25,000 playable items per library. Subtitle/audio selection and private watch history are supported. Metadata/poster
scraping, scheduled scanning, and direct-play/remux optimization are not implemented.

## Jellyfin apps and Quick Connect

The compatibility API lets Jellyfin clients use Media Center's existing libraries and
transcoders. It does not install a second media server or maintain a second catalog.

1. In the Jellyfin app, add `https://your-posterchan-host/jellyfin` as the server and
   select **Quick Connect**. The app displays a six-digit code.
2. Sign in to Posterchan with Nostr. Open **Media Center → Connect a Jellyfin app**,
   enter that code, and select **Approve this app**.
3. Return to the Jellyfin app to finish connecting. It sees the same shared libraries
   available to that Nostr identity. An administrator must first grant Media Center
   access and share the library, as described above.

Sharing is authorized by Nostr identity on the connected Posterchan server. The
recipient must sign in there, receive Media Center permission, and approve the app
under their own identity. The app then sees owned and explicitly shared libraries,
including the configured NAS proxy. No media catalog is federated to public relays.
Separate Posterchan servers require separate Jellyfin server connections; this does
not discover or merge remote libraries across unrelated servers automatically.

TV folder DTOs include stable display-preference IDs. Display preferences (32 client/folder
records plus user configuration, at most 48 KB per identity) and audio/subtitle defaults
are encrypted local events that survive reconnects. Explicit playback track choices
still override defaults. Favorites, played state, and progress share the latest-200-items
history bound per user/library.

TV artwork loaders can use a signed image tag without the full app token. A tag expires
within two hours, grants only that item's primary artwork, and is rechecked against
current device approval, Media Center permission, and library sharing on every request.
It cannot authorize browsing or streaming; image responses remain private and no-store.

This uses Jellyfin's [Quick Connect protocol](https://kotlin-sdk.jellyfin.org/guide/authentication.html#quick-connect).
There are no separate passwords. Codes expire after five minutes and are single-use.
App tokens last 90 days, survive restarts as hashed values in encrypted local events,
and grant only Media Center access. Up to 16 app sessions are retained per user.
**Disconnect all Jellyfin apps** revokes those tokens and pending approvals. Removing
Media Center permission or library sharing also blocks subsequent requests, including
playback already opened by an app. Pending codes and active playback sessions are
transient; after a server restart, request a new code or reopen playback as needed.

On a public-node/NAS setup, clients connect to the public node's `/jellyfin` URL.
The public node keeps app credentials locally and delegates media requests through
**Media Center Server URL**. Both nodes need the Media Center code. Catalogs, app
credentials and tokens are excluded from federation. Jellyfin clients use the same
200 KB/s default, GPU/CPU transcoders, `/tmp` cache and concurrency controls as web
viewers. Client bitrate requests can reduce available quality but cannot bypass server
limits. Bundled web clients can use token-authenticated CORS on `/jellyfin/*`; Nostr
approval endpoints retain Posterchan's existing origin restrictions.

Supported compatibility surface: server discovery, Quick Connect, current-user info,
library views, paginated item browsing/search, cover images, playback information,
HLS video/audio, playback session reporting, client WebSocket keepalives and logout. Direct-play/download endpoints
are not exposed because they would bypass Media Center's bandwidth controls.
The player offers audio-language selection and subtitles off/on. Embedded text subtitles
(including SRT and ASS) are converted to WebVTT; advanced ASS styling is not preserved.
Bitmap subtitles such as PGS are rendered into the transcoded video. Audio and subtitle
choices have separate segment cache entries and still obey the configured bandwidth cap.
Text extraction is cached and serialized, and playback continues while captions load.
Jellyfin apps receive track metadata and subtitle delivery URLs using the
[MediaStream API](https://typescript-sdk.jellyfin.org/interfaces/generated-client.MediaStream.html).

Jellyfin administration, plugins, Live TV, and remote control are not implemented.
Favorites and manual watched/unwatched changes use the same private user history. Playback progress is saved privately per user and library, with the latest 200 items retained. Android and the web player offer Resume or Start from beginning, and Jellyfin clients receive the saved position through UserData and Resume. There is no bundled Jellyfin web UI.
The official Android phone app receives a small Posterchan host page at `/jellyfin/`
when requesting HTML. It implements Android's deferred-script readiness handoff,
Quick Connect, saved media-only app login, library search, HLS playback, audio and
subtitle selection, and fullscreen. API discovery remains JSON. This page uses the
same Jellyfin adapter and enforced Media Center limits; no Jellyfin server or web
bundle is installed. Other clients that require the full Jellyfin web interface
remain outside the adapter's scope.
The API advertises the Jellyfin 10.11 playback protocol for client discovery while
identifying the server as **Posterchan Media Center**.

Validation uses the official `@jellyfin/sdk` 0.13.0 for discovery, Quick Connect,
browsing, cover retrieval, playback URL construction and stopping, with real FFmpeg
HLS decoding against both local and NAS-proxied libraries. The approval/disconnect UI
is exercised in Chrome at phone and TV sizes. The Android host additionally has a
phone-width browser regression covering its deferred handoff, approval, HLS playback,
saved-login reload and disconnect against local and NAS-proxied media. Individual Android phone, Android TV, iOS and other
packaged clients still need device testing; this is not a claim that every Jellyfin
client or feature works.

## Cloudflare

The web UI uses **`/api/media-center`**; Jellyfin clients use **`/jellyfin`**.
Bypass caching on both prefixes. Create a Cache Rule:

```text
(http.request.uri.path eq "/api/media-center") or
starts_with(http.request.uri.path, "/api/media-center/") or
(http.request.uri.path eq "/jellyfin") or
starts_with(http.request.uri.path, "/jellyfin/")
```

Set **Cache eligibility → Bypass cache**. Ensure later matching cache rules do not
override it. This covers catalog requests, HLS playlists and `.ts` segments. Responses
also send `Cache-Control: private, no-store` and `X-Accel-Buffering: no`; the service worker
excludes these paths from offline caches. Reverse proxies should honor the no-buffering
header so origin pacing reaches viewers. See
[Cloudflare Cache Rules settings](https://developers.cloudflare.com/cache/how-to/cache-rules/settings/).

Web-player URLs use expiring, media-scoped tickets. Jellyfin player URLs use
media-only app tokens and playback-session IDs. Every
playlist and segment request checks the current library ACL; revocation prevents future
requests, including existing tickets. Already-buffered media cannot be withdrawn.
Treat signed playback URLs as credentials and redact their query strings from access logs.
The Cloudflare rule must be configured in the zone; source changes do not create it.

## Validation

```sh
.venv/bin/pytest -q tests/test_media_center.py tests/test_media_center_packaging.py
node --check static/js/client/app.js
node --check static/js/client/sw.js
.venv/bin/python scripts/check_media_center.py
```

The broader regression command used for this validation is:

```sh
.venv/bin/pytest -q tests/test_media_center.py tests/test_media_center_packaging.py \
  tests/test_nip78_auth_privacy.py tests/test_admin_settings_coverage.py \
  tests/test_install_defaults.py tests/test_docker_nostr_no_models.py \
  tests/client/test_auth_signer_failure.py tests/client/test_nip78_auth.py
```

Compose tests require Docker Compose (or `MEDIA_TEST_COMPOSE=/path/to/docker-compose`).
The tests use isolated document stores, temporary databases and generated media. They
check login failure and stale-token recovery, Media Center permission grants/revocation,
cookie-free playback, encrypted permission hydration, existing-user schema upgrades,
installer preservation and Docker mounts, sharing/revocation, no federation, path confinement, bandwidth filtering and actual
byte pacing, stream admission, failure-safe scans, and real FFmpeg segments/cache reuse.
The browser check runs separate loopback public-proxy and NAS servers, real HLS playback,
phone/TV layouts, cover loading, seeking, full screen, two concurrent Nostr identities,
revocation and stream-slot release. It writes screenshots to `/tmp/pc-media-check`.

Validation on 2026-09-05: 86 focused tests plus 12 subtests passed across Media Center,
API/privacy/admin-schema, authentication, migration and packaging checks. The browser
check passed at the 200 KB/s per-viewer default. One-second tests of the actual Media
Center FFmpeg command on `nas.lan` passed with CPU libx264, NVIDIA NVENC and AMD VA-API.
AMF did not initialize on that host; AMD mode uses the working VA-API path first and
retains CPU fallback. Automatic mode selects NVIDIA first. The NAS's `/tmp` was verified
as a 32 GiB tmpfs. Retest hardware paths after driver or FFmpeg changes.

To include the official Jellyfin SDK integration cases (test-only dependency):

```sh
npm install --prefix /tmp/pc-jellyfin-sdk --no-audit --no-fund @jellyfin/sdk@0.13.0
JELLYFIN_TEST_SDK=/tmp/pc-jellyfin-sdk/node_modules/@jellyfin/sdk \
  .venv/bin/pytest -q tests/test_jellyfin.py
```

Without that environment variable, only the two SDK/FFmpeg cases are skipped; the
local/proxy Quick Connect, permission, revocation, token isolation, CORS, pagination,
bandwidth and relay-acknowledgement regression cases still run.

### Connected devices and visibility

Media Center is hidden by default for non-admins. Grant access from a user's
Profile → Permissions → Media Center. Both the sidebar and mobile More menu
hydrate that permission from the signed-in session; the API enforces it independently.

Under Media Center → Connect an app, Connected Jellyfin devices lists that user's
approved app tokens with device/app names and connection dates. Revoke one device
without disconnecting the others, or use Disconnect all. Older approvals without
metadata appear as “Jellyfin app”. Device records and playback progress are encrypted,
local-only Media Center events and survive restart. Neither changes the library ACL.
