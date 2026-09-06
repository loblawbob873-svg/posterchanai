# Phone signer regression — September 6, 2026

The DM delivery change in `4a7e7a7d3` registered live subscriptions before loading
history. That fixed a missing-subscription path, but its initial relay replay could
start decrypting historical messages before the shared encrypted DM cache loaded.
The regression test reproduced that ordering error against the deployed code.
Two clients using one phone could each submit six unnecessary historical decrypts.
The batch barrier also made five completed requests wait for the slowest sixth.

The correction retains immediate live subscriptions and post-EOSE delivery, while:

- holding historical replay until the shared-cache read finishes;
- allowing two independent history workers per external signer client, six for a
  local key, so one slow decrypt does not block the other worker;
- pausing history for 30 seconds after a failed decrypt and backing off that
  message's retry from one minute up to five minutes;
- keeping successful decrypts deduplicated and live arrivals outside the history
  queue, including operator-to-self notifications.

The original tests did not cover a history replay arriving while the shared cache
was still loading, or two clients sharing a slow signer. Those omissions were real;
a passing earlier suite did not establish that these cases worked.

## Verification

- 72 focused DM/signer/cache/auth/transport tests passed.
- The shipped-function harness covers the reproduced cache-ordering failure,
  post-EOSE delivery during cache loading, extension and Windows modes sharing a
  slow signer, independent worker progress, retry backoff, outage pause, and
  historical self-notification delivery without duplicate unread counts.
- Both DM harnesses passed on separately assembled desktop and mobile web bundles.
- The real-browser two-app QR test passed clock-skew, simultaneous pairing,
  signing and reload survival.
- A real-browser NIP-46 relay-restart test completed all 30 queued decrypts;
  signing recovered in 3.97 seconds and a fresh decrypt in 7.64 seconds. The signer
  and relay are isolated fixtures, not the user's physical phone. The test helper
  was corrected to check the expected user key and wait for signer readiness.
- Desktop, Android APK and Android emulator workflows now execute both DM
  harnesses against their bundled app.js, so source-only checks cannot conceal a
  stale packaged client.

Full pytest suite: **10,745 passed, 630 subtests passed, 25 skipped** in 26m33s.
Tested-file SHA-256 hashes verified unchanged before commit.

Deployed `febb0fe004d57e20067612bf19ffe25d652f3196` to server1, NAS and router.
Public app.js and sw.js match the tested commit. App, worker and media services
remain active without restart loops. Desktop 1.0.1492 and Android APK builds
passed their bundled DM gates. The published Windows installer SHA-512 matches
latest.yml; both DM harnesses also passed against app.js extracted from its ASAR.
The Gentoo desktop1492 payload checksum and overlay tests passed. Android emulator
checks completed with device=0 and instrumented=0 (both actually ran).

The other reported issues in `~/agent.txt` and the requested AI/Blossom/Live
Streaming policy preview and 15-minute enforcement follow the signer release.
