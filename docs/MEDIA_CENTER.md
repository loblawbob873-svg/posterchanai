# Media Center

Media Center scans server video and audio folders into a private library, sorted naturally
by folder and filename (Season 2 before Season 10). Open **Media Center** in the sidebar or
desktop launcher. A Nostr-signed-in administrator can add a folder; the owner can rescan
and grant or revoke access using npubs or hex public keys. Shared users sign in on the
hosting Posterchan instance. No social messages or invitations are sent automatically.

The source files stay on disk. Catalog pages, library ownership, sharing lists and limits
are NIP-44-encrypted, operator-signed kind-30078 events under `pcai:media-center:` in the
local relay. This namespace is excluded from public federation, disaster-recovery config
fan-out, and private mirror fan-out. Other Nostr clients do not independently discover or
decrypt this server-mediated library. Back up the local relay and operator storage key.

## Server setup

Install `ffmpeg` and `ffprobe`, and give the Posterchan service read access to the media
folder. The default allowed root is `$POSTERCHANAI_DATA/media`, or
`/var/lib/posterchanai/media` when that variable is unset. To allow existing media mounts,
set this environment variable in the service configuration and restart it:

```ini
POSTERCHANAI_MEDIA_ROOTS=/srv/media:/mnt/movies
POSTERCHANAI_MEDIA_CACHE=/var/cache/posterchanai/media-center
POSTERCHANAI_MEDIA_VAAPI_DEVICE=/dev/dri/renderD128
```

Only folders inside allowed roots can be added, and symbolic-link media entries are
excluded. The cache directory must be writable by the service; it defaults to
`/tmp/posterchan-media-center` with owner-only directory permissions. Cache segments are
ordinary playable bytes protected by the filesystem and API access checks, not encrypted
media blobs. Source media is not encrypted or uploaded by scanning.

Select automatic, CPU, NVIDIA, AMD, or VA-API transcoding when adding a library. CPU uses
libx264; NVIDIA uses NVENC; AMD tries VA-API then AMF. Automatic tries the hardware paths
before CPU. FFmpeg must include the encoder and the service must have access to the
corresponding drivers/device. Failed hardware attempts are suppressed for five minutes,
with CPU fallback. See [FFmpeg codec documentation](https://ffmpeg.org/ffmpeg-codecs.html).

## Bandwidth and resource controls

Administrators open **Bandwidth and resource limits** inside Media Center:

| Setting | Default | Meaning |
| --- | ---: | --- |
| Total bandwidth | 20,000 kbps | Combined video response-byte budget for all viewers |
| Bandwidth per user | 6,000 kbps | Combined budget for one Nostr identity, including multiple tabs |
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

Current bounds: 100 libraries and 10,000 playable items per library. This initial feature
does not include subtitle selection, watch-history synchronization, metadata/poster
scraping, scheduled scanning, or direct-play/remux optimization.

## Cloudflare

All API and streaming paths are under **`/api/media-center`**. Create a Cache Rule:

```text
(http.request.uri.path eq "/api/media-center") or
starts_with(http.request.uri.path, "/api/media-center/")
```

Set **Cache eligibility → Bypass cache**. Ensure later matching cache rules do not
override it. This covers catalog requests, HLS playlists and `.ts` segments. Responses
also send `Cache-Control: private, no-store` and `X-Accel-Buffering: no`; the service worker
excludes these paths from offline caches. Reverse proxies should honor the no-buffering
header so origin pacing reaches viewers. See
[Cloudflare Cache Rules settings](https://developers.cloudflare.com/cache/how-to/cache-rules/settings/).

Streaming URLs use expiring, media-scoped tickets instead of account tokens. Every
playlist and segment request checks the current library ACL; revocation prevents future
requests, including existing tickets. Already-buffered media cannot be withdrawn.
Treat signed playback URLs as credentials and redact their query strings from access logs.
The Cloudflare rule must be configured in the zone; source changes do not create it.

## Validation

```sh
.venv/bin/pytest -q tests/test_media_center.py
node --check static/js/client/app.js
node --check static/js/client/sw.js
```

The tests use an isolated in-memory document store and temporary generated media. They
check sharing/revocation, no federation, path confinement, bandwidth filtering and actual
byte pacing, stream admission, failure-safe scans, and real FFmpeg segments/cache reuse.
Hardware playback still needs validation on each target GPU and driver installation.
