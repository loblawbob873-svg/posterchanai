# Native wallet sends and clickable SMS links

This candidate adds Send controls and native transaction preparation, signing,
broadcast and status handling for BTC, LTC, DOGE, BCH, SOL and XRP. Receive
addresses, wallet/portfolio selection and the previously released portfolio
value, logos and chart presentation are retained. The supported native EVM and
independent XMR paths remain available. This is not full Exodus token/NFT or
hardware-wallet parity.

SMS download links are now clickable in both the web Texts app and Android's
native conversation view. Web rendering escapes text and links while preserving
the complete encryption-key fragment. Android binds fresh link spans to each
message and removes spans when a view is recycled. Large-file delivery itself
was already released and confirmed by the user.

## Review and regression evidence

- Full backend: 7,763 passed, 19 skipped, 519 subtests passed.
- Full client rerun: 3,468 passed, 1 skipped, 121 subtests passed. The first run
  exposed an obsolete assertion requiring BTC/LTC/DOGE/BCH/SOL to have no Send
  control. The corrected test checks all six native Send and Receive controls,
  plus disabled sending for unknown assets. The entire client suite was rerun.
- Exhaustive default runner coverage was checked by comparing discovered names:
  exactly 79 unique suite/check entries; 57 PASS, 21 SKIP, one CSS-scale advisory
  (612 existing findings). Raw reports are retained; the combined report explicitly
  records replacement of only the failed client row by the corrected full rerun.
- 51 wallet browser cases passed against source and each generated Android and
  desktop bundle, including actual native Send buttons and forms.
- 93 UTXO regressions passed after actual-provider DOGE/LTC contract corrections.
  Live read-only app-transport checks verified BTC/LTC/DOGE fee responses and the
  BCH mainnet anchor; a subsequent BCH output lookup timed out. No native-wallet
  expansion payment was sent with user funds.
- 427 combined SMS client/native regressions and 15 subtests passed after link
  integration. The browser media check performs actual pointer clicks at phone
  and desktop widths and checks the complete encryption-key URL.
- Android main and instrumentation Java compilation passed. Device verdict below
  is a separate requirement; compilation alone is not device evidence.
- The installer owner's commits 097a3960d, 76d785366 and 8451bdc26 were merged
  intact; 115 affected installer tests and 52 subtests passed. Uncommitted
  handoff/checkpoint work was not included.
- Final independent read-only integration review of d8b303fbe found no material
  wallet blockers. Earlier review fixed bounded subprocess cleanup, off-event-loop
  UTXO work, legacy BTC change derivation and live provider fee contracts.

XRP remains isolated in its dedicated SDK environment on both production nodes.
The application signer transports retain their existing websockets versions
(16.0 on server1; 16.1 on NAS). Uncertain broadcasts retain a durable lock and are
not automatically resent. Public RPC/provider availability remains a dependency.

The skipped default checks include installed physical desktop/Wayfire and ISO/VM
environments, missing SearXNG, and two fresh-account checks whose registration did
not complete. These were not passes. The separate published-artifact checks and
Android device result must be recorded below before claiming release completion.
No claim is made that physical Roku/handset behavior, every bank's autofill, or
real BTC/LTC/DOGE/BCH/SOL/XRP payments were tested.

Local evidence:
- /tmp/pc-wallet-final-combined.json (coverage and supersession provenance)
- /tmp/pc-wallet-final-unit.json
- /tmp/pc-wallet-final-client-checks.json (original failure preserved)
- /tmp/pc-wallet-final-client-rerun.json
- /tmp/pc-wallet-provider-live-regressions.log
- /tmp/pc-final-sms-combined-regressions.log
- /tmp/pc-final-installer-owner-integration.log

## Android device gate

Workflow 34071632847 on d29cf7cbf PASSED: downloaded XML records 92 tests,
zero failures, errors or skips. Both real SMS link tap and recycled-view tests
passed. Reports: /tmp/pc-wallet-final-device-report. This has the same wallet, SMS Java and
client runtime as final candidate d8b303fbe; the later merge changes installer
files only. Earlier runs 34070001300 and 34070995193 lost the emulator before
instrumentation and are not device-test passes. The fresh run uses supported
SwiftShader graphics with Vulkan disabled and retains host crash diagnostics.
The ten configuration/timeout tests pass; this infrastructure change is a
mitigation, not a proven diagnosis of the prior emulator exits.

## Deployment and published artifacts

Wallet/SMS runtime b8f2dc1f0 deployed successfully on server1, NAS and router.
Public exodus.js, sms.js, sw.js and exodus.css match the tested bytes. App and
worker services are active with zero automatic restarts on both application nodes.

Android 1.0.2215 (workflow34072468272) and desktop 1.0.1497
(workflow34072468215) are published. Public APK SHA256:
1ccbacc22874225b0592d86347f0631e63f41f2e30c5d7e89b4c963a6b4abcb4.
The APK's wallet, SMS and stylesheet assets match source. Desktop download SHA512
matches its published checksum. Each actual published bundle passed 54 wallet/SMS
runtime tests; the desktop document-app release gate also passed. The updated
1497 desktop overlay pin passes nine tests and is included in the next sync.

Read-only authenticated live wallet status/balances returned200. EVM/SOL/XRP
balances were known; UTXO discovery and independent XMR synchronization were
still in progress. This is not a claim that every live balance finished syncing.
No additional monetary payments were sent.
