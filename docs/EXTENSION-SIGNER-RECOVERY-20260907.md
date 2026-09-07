# Delegated extension signer recovery

The delegated NIP-46 transport could leave its shared opening promise unresolved
when a relay closed before opening, had no connection deadline, and did not replay
pending requests after disconnects or silent socket failures. A healthy relay
connection later did not rescue an already waiting app-session signature or DM
decrypt request.

The extension now dials its configured relays concurrently with a four-second
connection deadline and uses the first successful subscription immediately.
Pending requests can recover after disconnects, failed subscriptions, lost replies
and silent sockets. Retransmission reuses the exact signed event bytes and RPC ID;
it never generates a new signing request. Each relay receives at most the initial
publication plus four retries, spaced by 2/4/8/16-second delays. Socket replacement
does not reset this per-request budget. The existing 120-second approval deadline
is preserved, and no reconnect timer runs after requests settle.

Pairing changes/unpairing reject pending requests and retire old sockets. Delayed
callbacks cannot attach an old socket to a replacement session. Responses must be
valid signed kind-24133 events from the paired signer, addressed to the extension
app key and matching a pending request ID. NIP-07 origin/kind permissions remain
enforced by the existing background message handler. No account private key is
introduced into the delegated extension.

## Verification

- Baseline comparison: the unchanged background passed the healthy case but failed
  eight new cases for close-before-open, stalled dialing, close-after-publication,
  lost reply, zombie connection, app-session auth signing, and DM recovery through
  both signer encryption transports. Evidence:
  `/tmp/pc-extension-signer-red-before.json`.
- New source suite: **24 passed in 3.64 seconds**. It loads the shipped crypto and
  background code, uses a controlled relay, checks byte-identical replay, verifies
  signatures and decrypts real ciphertext. Actual NIP-07 message-handler tests
  cover kind-27235 auth signing and DM NIP-44 decryption; rejection and denied
  permission never become automatic approval.
- Both Firefox and Chrome packages built. The same scenario matrix passed against
  their background assets: **48/48**. Evidence:
  `/tmp/pc-extension-signer-bundled.json`.
- The 45-second simulated phone approval test succeeds while asserting exactly
  five publications, preserving approval time without flooding the signer.
- Repeated auth/DM calls before and after recovery used 17–30 ms of real in-process
  crypto/handler time in the Firefox harness, and 17–29 ms for Chrome. Healthy calls
  completed within one 100-ms virtual scheduling step; the interrupted auth call
  recovered at 2,000 ms. These are controlled harness measurements, not measured
  physical-phone or Internet latency.
- A mixed batch of 24 concurrent auth/DM requests after a close and dropped reply
  completed at 6,000 ms virtual time with 26 publications total; real harness time
  was 485 ms for Firefox and 475 ms for Chrome.
- JavaScript syntax and diff whitespace checks pass. Broader extension/full-suite
  validation and publication are coordinated separately by the release agent.

The tests also cover first-stalled/second-healthy relay selection, stale socket
callbacks, cancellation, unpairing, malformed signatures, wrong signer/recipient,
subscription-send failure, NIP-04 and NIP-44 transport, and concurrent recovery.

## Release review (extension 1.4.10)

Root reviewed the retry budget, unchanged approval deadline, session retirement,
subscription failure handling, and signed response validation. Rebuilt 1.4.10
Firefox/Chrome assets passed all 48 packaged scenarios again
(`/tmp/pc-extension-final-bundled.json`).

Full client suite: **3468 passed, 1 skipped, 121 subtests passed** (886.34 s).
Full backend run: **7810 passed, 17 skipped, 519 subtests passed**, with one
compile-harness failure. That harness still substituted a non-Service signer
stub for the new real-service Android device test. The correction compiles the
actual signer/crypto, retains the real push implementation for notification device
tests, and extends only external-library/JUnit compile shims. All four affected
compiler tests then passed (5.70 s). No runtime source changed after those full
suite runs; an additional full backend confirmation is running. These compile
shims do not execute the transport: the real OkHttp/Android test already passed in
two 93-test emulator runs documented in SIGNER-GRACEFUL-CLOSE-20260907.md.

Publication still requires checking the store result; a successful source deploy
alone does not update an installed Firefox extension.
