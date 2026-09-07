# Notification and editing fixes — September 7, 2026

Android Direct notifications now connect to the selected instance instead of the bundled asset origin. Readiness requires the server's authenticated ready frame. Blocked notification channels retain queued messages for retry; reconnects preserve receipt deduplication, and server queue wakes no longer discard in-flight acknowledgements. After updating an affected APK, Notification Test can repair its old registration using a fresh signed registration.

Quote posts containing only a valid NIP-18 q tag now reach local notification subscriptions, history and push generation. A derived index covers existing quotes without changing signed events or ordinary relay filter semantics. Historical quotes are discoverable in the list; old push alerts are not replayed.

Notes search updates only results, preserving focus, selection, IME composition and the editor. Notes and Edit Profile have refined CSS layouts and mobile controls. The final styling passes did not change editor or profile-save behavior.

## Review and validation

- Full backend run: **7,936 passed, 29 skipped, 519 subtests passed** (747.26 seconds).
- Full client run: **3,658 passed, one failed, one skipped, 121 subtests passed**. The single failure was a literal subscription-text lookup that rejected the new quote option. The lookup was repaired and its coalescing assertion strengthened; the final affected-feature run passed **35 tests**, including that regression, quote/Direct notifications, Notes search and profile save/reopen. Application code did not change after the full client run.
- Broad browser run: 51 passed, four failed, 19 skipped initially. The Media Center test needed an isolated membership fixture; its complete real FFmpeg/HLS, scan/move, sharing and concurrent playback rerun passed. Two installed-package checks passed after supplying the isolated checkout's missing readers; the document-package check correctly skipped because the locally installed package is older than this candidate. Remaining skips require external devices/services or supplied artifacts, not present in this environment.
- Android CI **34168987940**, source `e7aa3c585a968767b42d8daa5d0262cb5bf420a7`: device lifecycle command and instrumentation command both exited zero. XML confirms **101 tests, zero failures/errors/skips**, including `DirectPushDeliveryDeviceTest.authenticatesRendersAndAcksThenReconnectsWithoutDuplicateCards`. Final Android/client inputs are identical to this CI source; later changes only improve test fixtures and release documentation.
- Final native/device-source SDK compilation: **four passed**. Membership and deploy-mapping review: **83 passed**. Mapping alone: **29 passed**.
- JavaScript syntax and diff whitespace checks passed. CSS scale lint remains advisory.

The new Android test uses a real emulator, Keystore, OkHttp connection, notification renderer and receipt persistence. Its service context is attached directly; it does not independently establish Android-managed foreground-service startup or delivery through a physical carrier. Existing emulator lifecycle checks passed, but real-phone/carrier delivery remains a distinct validation limit.

## Deployment boundaries

The new quote helper is explicitly mapped to app, relay and worker consumers. This release must not restart media or terminal services. Client cache identifiers advance to `pc-nostr-v1689` and `posterchanai-v99`. Android and Windows packages must be rebuilt and their bundled assets checked against this source before claiming those packages contain the fixes.

The other agent's committed installer changes are retained in history. Its current installer/VM work and handoff files are excluded from this change. The separate read-only security audit's redirect findings are not fixed by this release.
