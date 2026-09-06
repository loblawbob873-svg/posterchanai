# Media Center compatibility review — 2026-09-05

Scope: TV folder browsing and private artwork, subtitle delivery and stream indexes,
client preference persistence, and private watched/favorite state. Review completed
before the service deployment of these changes.

## Findings addressed

- **TV folder crash:** the official Android TV `BrowseGridFragment.onCreate` calls
  `Objects.requireNonNull(mFolder.getDisplayPreferencesId())`. Library, root and
  nested-folder responses now carry stable preference IDs. Regression tests open
  the library, fetch child folders and their detail responses, and read/write each
  folder's preferences through both local and NAS routes.
- **TV artwork rejected:** the TV image URL builder carries the image tag, but its
  image loader does not send the app token. The adapter now signs an expiring,
  item-specific primary-image capability in that tag. Verification binds it to the
  approving session; device revocation, account permission and library ACL remain
  live checks. Tests cover missing/altered/expired tags, another item's URL,
  non-image endpoints, sharing removal, permission removal and logout.
- **Subtitle URL mismatch:** Kotlin's URL builder appends paths to the configured
  server base even when they begin with a slash. Subtitle URLs are now relative to
  the API root. The phone host resolves them against that same root. Browser tests
  load and display actual WebVTT cues while streaming HLS.
- **Sparse track indexes:** legacy TV code indexes the stream array by stream ID.
  Preserve omitted data/attachment positions and avoid colliding with an audio
  stream numbered zero. Validate indexes before reserving a playback session.
- **Client DTO completeness:** image-list responses include Kotlin's required Size
  field. An official SDK contract snapshot covers the additional response type.
  Unsupported optional collections/segments/parts return correctly shaped empty
  results; item-specific endpoints still resolve the caller's library access.
- **Persistence and input validation:** display/user preferences are bounded,
  encrypted local documents. Validate primitive types, UUID lists, enums and size
  before saving so malformed settings cannot break later TV deserialization.
  Configuration cannot change account policy, server settings or streaming limits.
- **Shared history isolation:** watched/favorite updates traverse the native NAS
  proxy and live library ACL. Progress updates merge with existing favorite state
  under the history lock. Another user's state remains independent.

## Verification

The local and NAS integration suite covers Quick Connect, official SDK discovery and
HLS decoding, folder navigation, image capabilities, settings hydration, sparse
tracks, language defaults, favorites, watched/resume state, sharing and revocation.
The real-browser checks cover phone/landscape fullscreen, active captions, decoded
selected-language audio, concurrent viewers, strict bandwidth pacing and cleanup.
Installer/Docker asset and Compose configuration checks use existing dependencies;
no new runtime package or configuration variable is required.

## Remaining verification limits

The installed TV APK has not been run by this environment. The missing folder
preference ID is a source-confirmed crash condition matching the user's report;
a device retest must confirm that it was the only failure on that version. Native
ExoPlayer playback, casting, downloads, background controls and other untested
Jellyfin clients are not established by the browser checks. This remains a scoped
Jellyfin API adapter, not an implementation of every Jellyfin server feature.

Shared Nostr identities are honored on the connected Posterchan instance and its
configured NAS. Catalogs are not federated and separate instances are not merged
into a single Jellyfin connection.

## Roku follow-up review

The user identified the affected device as Roku. Reviewed the released Roku 3.2.3
client rather than treating its behavior as Android TV behavior. Its unguarded
`MediaSources[0].Container` access reproduces the playback crash with the adapter's
old response. Item-detail requests now supply complete source and track DTOs;
browsing itself creates no native playback session and does not advertise direct
file access. The same builders generate PlaybackInfo to prevent contract drift.

The Roku image-list exception preserves the app's signed-tag fallback and remains
behind normal authentication and live library resolution. No image auth checks
were loosened. User confirmation established that Roku thumbnails now display.

Added per-platform request contracts for Roku, Fire TV and Google/Android TV plus
an optional test executing the official Roku source functions using `brs` 0.45.0.
The interpreter and checkout are test-only tools in /tmp; requirements.txt,
installer and Docker need no additional runtime dependencies for this correction.
Review completed before deploying the Roku follow-up. Physical playback confirmation
remains required after deployment.

## Concurrency and recovery follow-up

Roku playback and artwork are now confirmed working by the user. Fire TV and
Google TV remain covered by API contract tests, without physical device testing.

Review found that writing directly to the final segment-cache filename could
leave a partial cache hit after a killed process. Completed FFmpeg output is now
published with an atomic hard link under the existing cache lock, on the same
/tmp filesystem. Temporary-file cleanup removes the staging name; the completed
cache entry remains. This also avoids copying the segment a second time to disk.
No dependency, installer, Docker or configuration change is needed.

Regression coverage exercises repeated eight-viewer traffic, shared encoding and
its two-worker bound, enforced per-viewer bandwidth, disconnecting one or all
viewers, cancellation without charging unsent bytes, interrupted cache publication,
and cache reuse from a fresh Python process. Jellyfin recovery coverage drops
volatile state for local and NAS configurations and verifies that the existing
approved token can reopen the item with its saved position.

Recovery is not seamless playback through an app-server restart: active Jellyfin
play IDs are memory-only and return 404 afterward. Reopening the item obtains a
fresh play ID; device approval and saved progress remain available. These tests
use isolated fixtures, not disruptive restarts of production services. They do
not establish a multi-hour hardware soak or recovery behavior in every TV app.

Validation: the full Media Center, Jellyfin and packaging suite passed 113 tests;
the two additional local/NAS restart-recovery cases also passed. The extended
stress case passed 400 rounds with eight concurrent viewers (3,200 deliveries,
800 shared encode jobs) in 66.92 seconds, keeping the two-encode limit and the
default 200 KB/s per-viewer cap. Encoding is simulated in this stress case;
the full suite separately exercises actual FFmpeg output and client decoding.
Python compilation and whitespace checks passed. Review completed before rollout.
