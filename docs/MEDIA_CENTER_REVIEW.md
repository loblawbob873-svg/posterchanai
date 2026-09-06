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
