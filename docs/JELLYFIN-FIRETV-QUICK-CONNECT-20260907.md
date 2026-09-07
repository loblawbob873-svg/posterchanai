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

Deployed as19ea651a8 on all nodes. Actual public initiation and polling responses
were decoded successfully by both official Kotlin SDK versions. The dedicated
relay retained its pre-deploy activation timestamp; only APP and WORKER restarted.

Shared-library follow-up: the reported recipient already had Media Center enabled,
but the NAS Movies catalog had an empty sharing list. The owner's authenticated
sharing API was used to add the explicitly requested npub, preserving any prior
entries; a separate GET and NAS read verified access. The original empty list is
backed up privately under /tmp/pc-movies-sharing-before-20260907.json. A new
owner-save -> recipient web/TV access -> unrelated user denied -> revocation
round-trip passes for both local and NAS-proxied libraries (two tests). This does
not infer that an unsigned browser is logged in or bypass the account permission.

## Login-response follow-up

The first deployment fixed pairing initiation, but a physical TV attempt exposed
a second required field missing from AuthenticationResult.User:
HasConfiguredEasyPassword. Approval and redemption both returned200, after which
the TV failed to decode UserDto; repeated approval attempts then correctly
returned404 because the code had already been consumed.

UserDto now supplies HasConfiguredEasyPassword:false. The manual contract had
incorrectly marked this field optional; its flag and the other two required
password-status boolean flags are corrected. Actual compiled serializers from
both SDK versions were audited across86 models and597 fields. Other fixture
limitations and version differences are retained in the local audit report; no
additional emitted startup blocker was demonstrated.

The regression suite now runs actual official JVM SDK serializers against every
response in the existing full startup/browsing/playback contract, with both
authorization-header spellings and local/NAS-proxied libraries. Explicit
JELLYFIN_TEST_KOTLIN_MODELS configuration is required; absent jars are a reported
skip, not a schema-only pass. ApiModelDecode.java uses the SDK's own JSON settings.

Final affected suite:85 passed,4 skipped in108 seconds, with BOTH official Kotlin
SDK versions and the current official JavaScript SDK enabled. The remaining
skips require external Roku source/interpreter fixtures. Evidence:
/tmp/pc-firetv-full-sdk-regressions.log. This follows the full7770-test backend
run above; the only application follow-up is the additional response boolean.
The actual old authentication shape fails MissingFieldException under both JVM
SDKs; the corrected shape passes. A new TV code is required after deployment.

## Post-login crash diagnostics

Physical TV clients now pass login but reported a later crash. Their automatic
ClientLog/Document upload previously returned404, hiding the actual exception.
The adapter now accepts authenticated reports up to1 MB, retains only the first
8000 characters in one encrypted latest-report document per account, redacts the
active token and credential lines, and exposes no public log download. This is
diagnostic support, not a claim that the post-login crash is fixed.
Full affected suite with both real JVM SDKs and the JavaScript SDK:87 passed,
4 explicit Roku-fixture skips. Auth/revocation, upload limit, redaction and private
storage checks pass. Log: /tmp/pc-jellyfin-crash-receiver-full.log.

## Confirmed post-login TV crash: WebSocket message IDs

The Fire Stick's authenticated crash report identified a missing `MessageId` in
`ForceKeepAlive`, thrown by the official Kotlin SDK's socket decoder. Both server
heartbeat message types now include a fresh UUID. This also fixes the subsequent
`KeepAlive` response, which has the same required field. Authentication,
revocation, connection limits and heartbeat timing retain their existing behavior.

The expanded startup test opens the real adapter WebSocket after Quick Connect,
receives both message types and passes them through the official SDK's polymorphic
`OutboundWebSocketMessage` serializer on versions 1.7.1 and 1.8.12. The original
frame reproduced the physical Fire Stick's exact MissingFieldException before
applying the fix. These checks run for both supported authorization headers and
local/NAS catalog fixtures; the fallback test also verifies valid distinct UUIDs.

Evidence: `/tmp/pc-tv-socket-red.log` records the failing original frame;
`/tmp/pc-tv-socket-green.log` records 87 passed and 4 external Roku-fixture skips;
`/tmp/pc-tv-socket-polymorphic-final.log` records the stronger socket dispatch test.
The full backend run and public post-deployment socket verification are recorded
below when complete. Physical TV confirmation remains separate from SDK checks.

Full backend validation of the runtime fix: **7782 passed, 18 skipped, 519 subtests
passed** in 731.94 seconds (`/tmp/pc-tv-crash-full-backend.log`), with actual JVM
and JavaScript SDK dependencies enabled. The final polymorphic socket test
passed all 12 local/NAS/header/SDK combinations in 21.14 seconds. Review checked
both outgoing frame types, UUID format/uniqueness, and retained existing socket
authentication, revocation and capacity behavior; `git diff --check` passed.
