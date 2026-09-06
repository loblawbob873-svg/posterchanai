# Media Center client compatibility review

Reviewed 2026-09-05 against the official Android phone source
[`1c6fef33`](https://github.com/jellyfin/jellyfin-android/tree/1c6fef33cd3460e79cd2cbddd69ebe0b6a5a8d6c)
and Android TV source
[`49a5b6cd`](https://github.com/jellyfin/jellyfin-androidtv/tree/49a5b6cd39b5053184dad0d7de26d4175765d9a2).
The user's installed APK versions are unknown. Source review and browser tests
cannot replace a run on those devices.

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
| TV playback crash | The reported TV crash occurs after PlaybackInfo returns 200 and before any HLS request reaches the server. It remains unconfirmed without the device error/stack trace; do not describe it as fixed. |
| Physical devices | Test installed Android phone and TV APKs: connect, browse, start video, seek forward/back, switch audio, text/PGS subtitles, fullscreen, background/return and disconnect. The browser test proves HLS playback, not Android ExoPlayer behavior. |
| Library presentation | Jellyfin views expose each shared/owned library as a home-video collection, with nested folder DTOs, natural folder sorting, clean video names and private folder.png artwork. Android Back moves up one folder. TV image loaders that omit authentication may still fail to load private artwork; access checks remain enforced. |
| Watch history | Progress keeps playback sessions alive but does not persist watch position or played state. Resume returns an empty collection. Continue Watching, mark played/unplayed and favorites need encrypted per-user state and ACL checks. |
| Saved preferences | User/display preferences have valid default DTOs, but changes are not persisted. TV `DisplayPreferencesStore` attempts a POST when settings change. Add bounded encrypted preferences before claiming settings survive on native clients. |
| Advanced playback | Device profiles are not fully negotiated; the adapter supplies H.264/AAC HLS within configured caps. Chapters, intro/credits segments, special features and multi-part items are not modeled. |
| Android host features | The small page provides Quick Connect, search, library/item cards, WebView HLS, audio/subtitles and fullscreen. It does not implement Jellyfin's entire JavaScript plugin API, native ExoPlayer integration, casting, downloads or background media controls. |
| Restart during pairing | Unredeemed Quick Connect codes are memory-only and expire on app restart. Approved app tokens are encrypted and survive restart. |
| Other clients | iOS and clients requiring Jellyfin's full web/plugin runtime remain unverified. No Jellyfin server or web bundle is installed. |

## Test command

```sh
JELLYFIN_TEST_SDK=/tmp/pc-jellyfin-sdk/node_modules/@jellyfin/sdk \
  .venv/bin/pytest -q tests/test_jellyfin.py
```

The SDK is a test-only dependency. Browser integration needs Chrome and FFmpeg;
those tests skip if the tools are absent. Android-host coverage includes native-bridge fullscreen/Back, browser fullscreen, active WebVTT cues on/off, and an audio-language switch verified by decoding the selected HLS audio tone. Browser fixtures contain generated media
and isolated users, never a copy of the production library or app credentials.
