# Jellyfin SDK 1.9.0 source review — 2026-09-08

The official Kotlin SDK [v1.9.0 release](https://github.com/jellyfin/jellyfin-sdk-kotlin/releases/tag/v1.9.0), published at 08:40:36 UTC, appeared between two project checks. Its changes explain the 20 tracked Kotlin model hash differences. The JavaScript SDK remains 0.13.0 and the tracked Roku files are unchanged.

## Reviewed model changes

Reviewed the [v1.8.12…v1.9.0 comparison](https://github.com/jellyfin/jellyfin-sdk-kotlin/compare/v1.8.12...v1.9.0), including every changed model tracked by `tests/jellyfin/upstream.json`:

- `EncodingOptions`: `EncoderPreset` becomes required; adds required `SubtitleExtractionTimeoutMinutes` and defaulted `HlsAudioSeekStrategy`. The adapter already emits `auto` and `2` for the required fields. The existing union contract includes these fields and the new `HlsAudioSeekStrategy` enum.
- `MediaStream`: adds required `IsOriginal` and optional localized language/original labels. The adapter already emits a Boolean `IsOriginal`; the union contract already requires it.
- `BaseItemDto`: permits null inner trickplay maps; adds optional album normalization gain and original language. Existing responses remain valid.
- `UserDto`: deprecated password flags become nullable/defaulted. Continue emitting the flags required by older clients.
- `SessionInfoDto`: removes optional `NowPlayingQueueFullItems`. Tolerant SDK deserialization ignores the retained older-client field.
- `TranscodingProfile`: deprecated `BreakOnNonKeyFrames` becomes nullable with its existing default. Existing Boolean responses remain valid.
- `PersonKind` adds `Narrator`; `TranscodeReason` adds `VideoRotationNotSupported`. These additions do not invalidate existing emitted values.
- Documentation-only changes: `CodecType`, `EncodingContext`, `MediaSourceInfo`, `MediaSourceType`, `PlayAccess`, `PlayMethod`, `PlaybackErrorCode`, `QueueItem`, `RepeatMode`, `TranscodeSeekInfo`, and `TransportStreamTimestamp`.

No adapter field changes are required by this review. The 20 baseline hashes now record the reviewed release; the stricter existing union schema and older SDK tests remain intact.

## Compatibility boundary and execution

SDK 1.9 raises its recommended discovery minimum to **server 12.0** and reorganizes API operations, including authentication and deprecated HLS requests. Passing its model deserializers does **not** prove a complete SDK 1.9 client can connect. Do not raise the adapter's advertised `10.11.11` merely to bypass that gate.

Released Android TV 0.19.10 pins SDK 1.7.1 (minimum server 10.10); the reviewed Android TV master pins 1.8.12 (minimum 10.11). Both accept the existing advertised version. Their actual JVM decode matrix passed previously: 12 cases including the four schema cases. Physical testing of this deployment candidate remains separate.

The new 1.9.0 DTO-only matrix is run separately before release acceptance. Official Maven dependencies are staged at `/tmp/pc-firetv-kotlin-models/1.9.0`: model 1.9.0, Kotlin stdlib 2.4.20, serialization JSON/core 1.11.0, and annotations 13.0. Persistent reviewed upstream sources are at `/tmp/pc-jellyfin-upstream-review-20260908`. The release owner records the completed matrix and affected gate results; this source-review document does not substitute for their execution.

Completed validation: the actual JVM/schema matrix passed all **16 cases**, with no skips, in 25.91 seconds. The official upstream integration check then passed **87 cases** in 91.14 seconds and accepted the reviewed hashes. Its 12 JVM cases were skipped because that invocation omitted the jar environment; all 12 ran successfully in the separate matrix. Together these runs exercise all 99 cases in `tests/test_jellyfin.py`.
