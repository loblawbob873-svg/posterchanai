# Regression release verification, September 6

This release prioritizes extension/phone signer responsiveness, complete Android
password synchronization, SMS notification readiness, Concord social emoji rendering,
and terminal replay/UTF-8 correctness. Wallet expansion remains isolated and is not
part of this release. The independent agent's committed installer/Steam work is
included; their uncommitted gentoo.sh and scratch-test changes are excluded.

Review found an Android vault logout race: native cache clearing waited for a prior
JavaScript write acknowledgement, which a page reload could discard. Capacitor's
single native handler already orders the synchronous put and clear methods. Clear
now dispatches immediately; retired-owner guards prevent further writes. A runtime
test held the write acknowledgement and failed before this correction. It now
passes against source and the generated desktop bundle.

Verification evidence (local logs are retained under /tmp):

- Full backend suite: 7,402 passed, 19 skipped, 519 subtests passed.
- Full client rerun: 3,403 passed, 2 skipped, 111 subtests passed.
  The initial run failed two assertions expecting pre-deduplication terminal text;
  corrected assertions verify newly displayed history and the active scroll guard.
- Vault follow-up: 157 focused tests passed; actual browser checks passed at
  390, 360, 900 and 1280 pixels; generated desktop vault runtime passed.
- Android emulator workflow 34061102226: 88 tests, zero failures/errors/skips.
  This runs the unchanged Android native code preceding the final vault JS fix;
  native release workflows also test the final packaged vault JavaScript.
- Browser suite included real signer transport, relay bulk recovery, Jellyfin scan
  reconciliation, first-run ownership proof, OS window controls and terminal checks.
- The full browser run had one intermittent second-relay signer login timeout.
  Both subsequent complete signer scenario runs passed, as did a separate repeat
  of the second-relay case. The original failure is retained; its cause is not
  established and these reruns do not prove it cannot recur.
- Review corrected the reconnect diagnostic's stale login-helper calls so it
  supplies the expected signer identity and waits for bound login controls.

Logs: /tmp/pc-regressions-final-suite.log, /tmp/pc-terminal-final-client.log,
/tmp/pc-vault-clear-review-tests.log, /tmp/pc-vault-final-browser.log,
/tmp/pc-final-bundle-build.log, /tmp/pc-signer-final-review.log,
/tmp/pc-signer-repeat-review.log, /tmp/pc-signer-final-stress.log.

The initial aggregate report is not an all-green report. Its failed tests were
investigated and rerun as described above. Installed physical desktop, Roku,
handset and ISO checks unavailable on this host were not verified by these runs.

The corrected reconnect diagnostic passed all scenarios: relay restart (732 ms),
relay lost during a request (3 ms), signer temporarily absent (6.1 s), and an open
socket silently discarding traffic (6.0 s). The proxy scenario waits for startup
signer traffic to finish before freezing the connection, isolating loss of the
probe request from loss of an earlier login response. Log:
/tmp/pc-signer-reconnect-drained.log. The earlier undrained run failed and remains
in /tmp/pc-signer-reconnect-corrected.log; this fixture distinction is explicit.
