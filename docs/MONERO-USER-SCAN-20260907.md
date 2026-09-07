# Scan in the regular-user Monero Send form

The operator wallet already had Scan wallet QR. The separate per-user wallet
form added in 0c6422015 omitted it. The per-user form now exposes the same native
ZXing/browser scanner, with its own wallet network and destination/amount fields.
A pasted monero: payment URI also populates the form before review. Scanning
never confirms or sends a payment.

The shared scanner allows one active session per modal. Cancelled or closed
modals stop late camera streams before playback. Cleanup is tied to the active
session so an older cancelled permission request cannot remove a replacement
session's guard or hide its camera view. Missing/denied camera access retains
an explicit paste-URI fallback. No native plugin or backend change was needed.

Validation before deployment:

- Eleven driven scanner cases cover native QR values, exact decimal amounts,
  wallet-network rejection, pasted URI review, cancellation/failure, missing or
  denied camera access, duplicate clicks, late permission and stale cleanup.
  Every case asserts that scanning creates no payment request.
- Existing wallet send and responsive-layout checks: 68 passed in 115.77s.
- Independent review caught and verified the stale-session cleanup correction;
  no remaining scanner blocker was found.
- Generated Android and desktop bundles each passed all 36 combined auto-mute,
  payment and scanner cases (79.39s and 79.36s).
- Full client confirmation: 3528 passed, 1 skipped, 121 subtests in 957.06s.
  The unchanged backend passed 7811 tests and 519 subtests in the preceding
  release; its Android emulator run passed all 93 native tests.
- Published-bundle checks follow deployment.
