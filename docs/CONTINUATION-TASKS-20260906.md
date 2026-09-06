# Continuation task ledger — September 6, 2026

The prior splash/security/Roku/media/DM work and integrated wallet commits are
recorded in SECURITY-AUDIT-20260906.md and CONTINUATION-20260906.md.

## Priority signer correction

Committed and deployed febb0fe00; full suite 10,745 passed plus 630 subtests,
25 skips. Windows1492 installer tested; Android APK published; emulator checks actually ran and passed. See SIGNER-REGRESSION-20260906.md for limitations and evidence.

## Follow-up fixes deployed in 68799fa95

- Account sign-up visibility respects the instance registration switch, retaining
  local identity creation in standalone mode and existing-user login.
- Concord's shared emoji picker receives the pre-hide button rectangle, stays near
  the message at phone/desktop widths and page scales, and preserves reaction target.
- Jellyfin device rows reserve readable name width beside Revoke.
- Media Center refresh rebuilds directories after rescans; catalog revisions include
  content hashes, including equal-count scans completed in the same second.
- Scan move/rename/deletion and unreadable-subtree preservation covered by API tests;
  actual browser/HLS scan reconciliation check passed.
- First-run fixture validates real challenge/action/payload-bound Nostr signatures.
  First-run browser check and OS window-control reachability check pass.
- Welcome splash defers during OS/child-window/setup use, including asynchronous
  entry and already-open dialog; browser regressions pass.
- Policy preview executes on first click, preserves edits during loading, shows
  affected accounts and errors, and includes Live Streaming counts.
- Policy revokes can_stream along with AI/Blossom, preserves exemptions, persists
  authoritative documents, and runs once per worker every 15 minutes when enabled.
  Actual OBS auth denies an existing key after cleanup; public reads stay open.

## Still required

- Terminal correction implemented: retain discovered hosts on remount/transient failures,
  reset the one-shot latch, retry discovery and isolate account/instance responses.
  Runtime regression harness and local-terminal tests passed; native CI checks bundled term.js.

- The follow-up stage above passed the full suite: 10,787 tests plus 632 subtests, 25 skipped. Deployed to server1/NAS/router; public app/term/SW bytes verified.
- Preserve/reconcile other agent's new os/gentoo.sh installer edits; no blanket staging.
- Wallet presentation: asset logos, graphs, total portfolio value and readable layout.
- Wallet/portfolio selection with isolated balances, receive/send state and stale
  response handling. User also requested support like Exodus's other portfolios;
  wallet scope is confirmed: custom CloudOS wallet, two-way Exodus recovery compatibility, no official app required. Monero must be independent of the built-in wallet.
- Wallet correctness/browser tests, code review, full suite and deployment.
- Verify final Windows/Android published bundles and emulator outcomes.

Do not call physical phone/Roku/Windows use verified based only on fixtures or
bundled-byte tests. No unreviewed installer/ISO publication. Independent agent's
wallet/Concord history is preserved; session coordination is in AGENT_HANDOFF.md.

## New regressions reported during continuation

- Signer: interactive authentication now has priority and a reserved slot during extension
  decrypt restoration. Startup starts the shared authenticated session before background reads.
  Runtime test queues 200 decrypts and verifies authentication starts before any complete.
- Android autofill: cold browser cache contained only printer while Chase/Wells Fargo remained
  on relay. Background vault sync now runs at startup/reconnect/resume; incomplete reads cannot
  overwrite the native snapshot. Logout/account-switch late answers are rejected. No background
  key creation. Tests run shipped vault/core with encrypted entries without rendering Passwords.
- SMS: native ThreadListActivity lacked POST_NOTIFICATIONS request (WebView had it).
  Native request added; readiness checks runtime permission, app switch and SMS channel.
  Blocked native alerts link to Android settings. Production and instrumented sources compile.
  Physical handset notification state has not been observed; do not claim it was verified.
- Concord: messages bypassed social custom emoji rendering and CORD rumor signing bypasses the
  public publish tag enrichment. Shared social emoji renderer/loading/tag generation now wired;
  runtime tests include DRC_emojo, authoritative event URLs and preserved HTML attributes.
- Each new client runtime check also runs against the Windows/Android packaged JavaScript.
- Terminal duplicate text/input with Codex over GNU screen remains under investigation.
- Wallet UI/switching changes and recovery derivation work remain isolated in
  /tmp/pc-wallet-followup-worktree; not deployed or fully verified for the final user scope.

New regression stage still needs the full suite, native emulator run, final review and deployment.
Do not commit gentoo.sh or the other agent's unfinished installer/overlay edits.


## Current release status (September 6, evening)

The later regression release supersedes the pending status above. Signer priority,
background autofill sync, SMS permission/readiness, DRC emoji rendering and terminal
replay/UTF-8 corrections are deployed in 1297393e6. Review and full-suite evidence,
including retained intermittent-test limitations, is in REGRESSION-RELEASE-20260906.md.
Desktop 1494 packaged checks passed. Android 2212 release succeeded; its public APK
matches the mirrored release SHA-256
`8f82d4f648882b367684bd3d06169d7d28f31674ffb805df7ab5f7453e8e0077`.
The final Android emulator report contains 88 tests, zero failures/errors/skips.
These do not claim physical handset or Roku verification.

Monero pooled output replenishment and the worker restart mapping are deployed in
188348034. The separate operator spending-cap outage was caused by five mocked test
charges in the live ledger. They were backed up and individually removed; production
limits were retained and live payment preparation succeeds without sending funds.
Hard test-ledger isolation is deployed in e85ef3cb2; all 386 Monero/deployment tests
pass. See MONERO-ZAP-REGRESSION-20260906.md for the incident evidence.

Wallet expansion remains isolated with the wallet agent. Its integrated full backend
suite has passed; remaining client/browser checks and imported-address discovery
review are ongoing. Independent Monero runtime configuration is staged on both
application nodes, without activating the new wallet code. Non-EVM/non-XMR sending
remains unfinished; do not describe the isolated wallet as complete Exodus parity.
Other agent installer changes remain preserved and excluded from these deployments.
