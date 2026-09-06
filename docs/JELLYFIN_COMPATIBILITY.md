# Media Center client compatibility review

Reviewed 2026-09-05 against the official Android phone source
[`1c6fef33`](https://github.com/jellyfin/jellyfin-android/tree/1c6fef33cd3460e79cd2cbddd69ebe0b6a5a8d6c)
and Android TV source
[`49a5b6cd`](https://github.com/jellyfin/jellyfin-androidtv/tree/49a5b6cd39b5053184dad0d7de26d4175765d9a2).
The user's installed APK versions are unknown. Source review and browser tests
cannot replace a run on those devices.

## Explicit TV targets

- **Amazon Fire TV and Google/Android TV:** the official Android TV client family
  ([official client listing](https://jellyfin.org/downloads/clients/all/)). Existing
  Kotlin DTO, folder, image, Quick Connect and HLS contracts cover this family.
- **Roku:** reviewed against official release
  [3.2.3](https://github.com/jellyfin/jellyfin-roku/tree/3.2.3), separately from Android TV.
  The user confirmed thumbnails now load. Roku's `getContainerType` reads
  `MediaSources[0].Container` from item details; the previous empty array crashes
  before HLS starts. Item details now provide a source and track metadata without
  creating a playback session. Source/track contracts are shared with PlaybackInfo.
  Catalog lists omit unrequested source details to keep browsing inexpensive.
- **Android phone:** keep the existing hosted player, fullscreen, subtitle/audio,
  Quick Connect and resume browser regression coverage.

Roku's `PosterImage` helper ignores ImageTag in image-list results and overrides the
signed-tag URL provided by `VideoData`. For authenticated Roku image-list requests,
return an empty list to select its built-in `ImageTags.Primary` fallback. Other
clients retain their ordinary image-list DTOs. Images still require an app token or
valid, item-scoped image capability; no unauthenticated artwork bypass is added.

The actual reported TV is Roku, superseding the earlier Android TV assumption.
A BrightScript interpreter executes the release's original container and poster
functions against fixture API responses: the previous response fails and the new
response succeeds. This is stronger than a JSON shape check, but it is not a run
on physical Roku hardware. Amazon/Google native player hardware also needs device
validation before claiming every playback mode works.

## Connection and playback blockers addressed

| Client path | Finding and implementation | Verification |
| --- | --- | --- |
| Server discovery | Accept root/trailing slash and case-insensitive API routes; advertise the public HTTPS address behind the proxy. | HTTP contracts and official JavaScript SDK discovery. |
| Quick Connect | Android serializes `secret` in camelCase. Accept both forms; approval remains Nostr-authenticated and redemption is single-use. | Local and NAS-proxy tests, including token revocation. |
| TV startup | Kotlin requires fields JavaScript tolerates omitting. Complete user configuration, policy, preferences, session and stream DTOs. Provide the read-only encoding capability response. | Snapshot of official Kotlin model requirements, recursively validated for startup and playback responses. |
| Android phone startup | `WebViewFragment` loads the server root and waits for `main.*.bundle.js` interception before declaring connection success. A JSON root never completes that handoff. | HTML content negotiation plus the small Posterchan host page; browser test simulates the native deferred script reload. |
| Android phone login | Native host reads `jellyfin_credentials` on the capabilities request. Save the media-only login in that format before reporting capabilities. | Real browser approval, credentials check, authenticated browse and reload without another approval. |
| TV playback source selection | `JellyfinMediaStreamResolver` filters for `Protocol=File` and `IsRemote=false` before choosing transcoding. Describe the backing file accordingly; expose only an HTTP HLS playback URL and no filesystem path. | Explicit source-selection regression, SDK HLS decoding and browser playback. |
| TV stream changes | `PlaybackManager` calls `DELETE /Videos/ActiveEncodings`. Implement idempotent, token-scoped native-session release. | Missing auth and another token cannot release the stream; repeated cleanup succeeds. |
| Bandwidth and access | All app streams use native Media Center tickets, live ACL checks, allowed profiles and byte pacing through the same NAS proxy. | Existing native streaming and Jellyfin integration coverage; no direct-file playback advertised. |

## Remaining gaps

| Area | Current behavior / next work |
| --- | --- |
| TV folder crash | User narrowed the failure to opening folders. `BrowseGridFragment.onCreate` requires a non-null `DisplayPreferencesId`; library/folder DTOs now supply stable IDs. TV image loaders receive scoped artwork tickets, and optional collection/image/parts endpoints return valid DTOs. Local/NAS regression tests pass; the installed TV APK still needs a device retest. |
| Physical devices | Test installed Android phone and TV APKs: connect, browse, start video, seek forward/back, switch audio, text/PGS subtitles, fullscreen, background/return and disconnect. The browser test proves HLS playback, not Android ExoPlayer behavior. |
| Library presentation | Jellyfin views expose each shared/owned library as a home-video collection, with nested folder DTOs, natural folder sorting, clean video names and private folder.png artwork. Android Back moves up one folder. TV image loaders that omit app authentication use short-lived, item-specific signed image tags; live ACL and device revocation checks remain enforced. |
| Watch history | Progress and played state are encrypted per user/library on the media backend, with the latest 200 items retained. Clients save periodically and on pause/stop; Jellyfin Resume and UserData hydrate that history. Android and the native web player prompt Resume or Start from beginning. Manual mark played/unplayed and favorites persist through the same bounded private history; progress updates preserve favorites. |
| Saved preferences | User configuration and client/folder display preferences persist in bounded encrypted local events. Types, UUID lists, enum values and size are validated before saving; preferences hydrate for new app sessions. Language defaults apply when playback does not explicitly select a track. |
| Advanced playback | Device profiles are not fully negotiated; the adapter supplies H.264/AAC HLS within configured caps. Chapters, intro/credits segments, special features and multi-part items are not modeled. Sparse native stream indexes retain array positions for legacy TV code. Subtitle URLs are API-relative because Kotlin appends the server base path itself. |
| Android host features | The neon-themed phone page provides Quick Connect, search, folder/item cards, saved-progress prompts, WebView HLS, audio/subtitles and native-bridge fullscreen. TV colors/layout are controlled by the installed APK, not server CSS. It does not implement Jellyfin's entire JavaScript plugin API, native ExoPlayer integration, casting, downloads or background media controls. |
| Restart during pairing | Unredeemed Quick Connect codes are memory-only and expire on app restart. Approved app tokens are encrypted and survive restart. Media Center lists connected devices with app/name/date and offers individual revocation or Disconnect all. |
| Other clients | iOS and clients requiring Jellyfin's full web/plugin runtime remain unverified. No Jellyfin server or web bundle is installed. |

## Test command

```sh
JELLYFIN_TEST_SDK=/tmp/pc-jellyfin-sdk/node_modules/@jellyfin/sdk \
  .venv/bin/pytest -q tests/test_jellyfin.py
```

The SDK is a test-only dependency. Browser integration needs Chrome and FFmpeg;
those tests skip if the tools are absent. Android-host coverage includes native-bridge fullscreen/Back, browser fullscreen, active WebVTT cues on/off, and an audio-language switch verified by decoding the selected HLS audio tone. Browser fixtures contain generated media
and isolated users, never a copy of the production library or app credentials.
