# Phone signer recovery after a relay restart

The native OkHttp listener handled failure and completed close callbacks but did
not acknowledge a server-initiated close. A relay restart could therefore leave
the phone signer in a closing state without reconnecting until its heartbeat.
Commit `f6084a67f` acknowledges the close and schedules the existing bounded
reconnect immediately. Every terminal callback retains the current-socket identity
guard, so callbacks from a retired connection cannot remove its replacement.
Reserved no-status code 1005 is normalized to legal reply code 1000.

The device regression uses real OkHttp 4.12 sockets, Android Handler execution,
isolated test-key preferences and a local MockWebServer relay. It checks both
signer and SMS subscriptions, server close 1001, reconnection without app reload,
late closing/closed/failure callbacks, no-status normalization, and a correctly
signed and encrypted NIP-46 public-key request/reply after reconnection. No user
keys or live payments are involved. MockWebServer is an Android-test dependency.

## Verification

- Red-before commit `77e598842`, run **34073156608**: 93 device tests, one failure,
  zero errors/skips. The new regression failed waiting for the replacement
  subscription, demonstrating the missing reconnect before the production fix.
- First fixed run **34073682334**: the test relay's own cleanup omitted its close
  acknowledgement; MockWebServer shutdown timed out. Test-only commit `1d3360001`
  acknowledges cleanup closes, waits for bounded handshake completion and preserves
  primary assertion failures if cleanup also fails. Production fix unchanged.
- Run **34074518515**, attempt 1: emulator disappeared while leaving Doze;
  lifecycle exit 124, instrumentation exit 2. No instrumentation tests ran and no
  XML was produced. This attempt provides no signer test result.
- Run **34074518515**, attempt **2**, exact head
  `1d33600018638165c12b0ee6bc57f38bf92c4523`: **93 passed, 0 failures, 0 errors,
  0 skipped**, verified from the downloaded `androidTest-report` XML. The signer
  reconnect regression passed in **4.743 seconds**. Lifecycle and instrumentation
  both exited zero; APK build, bundled composer checks and asset provenance passed.
- Local signer/core and real-android.jar compilation checks:
  `tests/test_android_signer_service.py` and
  `tests/test_android_signer_service_compiles.py`: **42 passed in 10.92 seconds**.

Downloaded evidence for attempt 2:

- `/tmp/pc-signer-green2-attempt2-report/debug/TEST-emulator-5554 - 14-_app-.xml`
- `/tmp/pc-signer-green2-attempt2-report/pc-instrumented.log`
- `/tmp/pc-signer-green2-attempt2-device.log`

The device gate verifies the native fix on Android 14. It does not claim a tested
production rollout to users' phones; release packaging and deployment are a
separate step coordinated by the release agent.
