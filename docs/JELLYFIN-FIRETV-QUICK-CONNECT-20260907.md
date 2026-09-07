# Fire TV and newer Jellyfin Quick Connect compatibility

Jellyfin Android TV 0.19.10 uses Kotlin SDK 1.7.1. Its QuickConnectResult requires
DeviceId, DeviceName, AppName and AppVersion in addition to the pairing secret,
code, authentication state and creation date. The adapter omitted those four
fields. The client catches that decoding error, marks Quick Connect unavailable,
and displays the username/password flow instead.

The adapter now returns those fields for both initiation and polling, captures
the requesting device ID with the existing bounded metadata sanitizer, and uses
string defaults when metadata is absent. POST initiation, signed account approval,
expiry, single-use redemption and media-only permissions are unchanged. No legacy
GET route was added: the initial GET-compatibility hypothesis does not apply to
the user's 0.19.10 client, which uses POST.

Validation:
- Regression failed before the fix on local and NAS-proxied configurations because
  DeviceId was missing. Both authorization-header spellings, anonymous metadata
  defaults, initiation, pending polling, approved polling, login and playback
  contracts are covered after the fix.
- Full backend rerun: 7,770 passed, 18 skipped, 519 subtests passed in 751 seconds.
  This includes the current official JavaScript SDK integration with real FFmpeg
  decoding (JELLYFIN_TEST_SDK set). SearXNG environment check remained skipped.
- Official Kotlin JVM SDK 1.7.1 AND 1.8.12 were executed against an actual new
  adapter response. Both decode it; both reject the old missing-field shape with
  MissingFieldException. tests/jellyfin/QuickConnectDecode.java is the repeatable
  decoder driver. Model/runtime jars were isolated under
  /tmp/pc-firetv-kotlin-models; production dependencies were not changed.
- The required model source is byte-identical in both SDK versions (SHA256
  3ed8adedefe7a5bb04e8b60c7265ad6361b0363d3c1216ba933d422b8e8ede25).
  QuickConnectResult was added to the existing upstream drift baseline and
  contract fixture so this startup response is no longer omitted from review.
- Independent read-only review of f4c8ff2fa found no compatibility/auth blockers.
- Client assets and Android runtime are unchanged from the wallet/SMS release
  with 3,468 client tests and 92 Android device tests already passed.

This is SDK and adapter evidence, not a claim of a physical Fire Stick/smart-TV
run. Public deployment verification follows separately.

Primary source contracts:
- https://github.com/jellyfin/jellyfin-androidtv/blob/v0.19.10/gradle/libs.versions.toml
- https://github.com/jellyfin/jellyfin-androidtv/blob/v0.19.10/app/src/main/java/org/jellyfin/androidtv/ui/startup/UserLoginViewModel.kt
- https://github.com/jellyfin/jellyfin-sdk-kotlin/blob/v1.7.1/jellyfin-model/src/commonMain/kotlin-generated/org/jellyfin/sdk/model/api/QuickConnectResult.kt
- https://github.com/jellyfin/jellyfin-sdk-kotlin/blob/v1.8.12/jellyfin-model/src/commonMain/kotlin-generated/org/jellyfin/sdk/model/api/QuickConnectResult.kt

Evidence: /tmp/pc-firetv-final-unit.json, /tmp/pc-firetv-quick-connect-red.log,
/tmp/pc-firetv-quick-connect-tests.log.
