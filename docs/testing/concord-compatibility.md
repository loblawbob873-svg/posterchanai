# Concord compatibility audit

Audited 2026-09-15 against the normative [Concord specifications at
b84554ea5dd47510057a580fa2f8587b4399ad17](https://github.com/concord-protocol/concord/tree/b84554ea5dd47510057a580fa2f8587b4399ad17).
The upstream repository describes an evolving protocol. Compatibility must be
qualified by this revision; passing our own round-trip tests does not establish
interoperability with every revision or client.

## Coverage and gaps

This matrix includes the reviewed core, membership, expiry, private-channel,
direct-invite, rekey/refounding, creator-link and CORD-07 integration series.
It describes the implementation under release review, not confirmation of a
production deployment. The resolved findings and remaining verification limits below qualify these
claims. Passing local suites is not a certification of universal interoperability.

| Spec | Implemented and covered by regression tests | Remaining work |
| --- | --- | --- |
| [CORD-01](https://github.com/concord-protocol/concord/blob/b84554ea5dd47510057a580fa2f8587b4399ad17/01.md) streams | NIP-44 self-ECDH, signed persistent/ephemeral wraps, encrypted/plaintext seals, rumor hashes, author/channel binding, layer-size caps, wire/capability-scoped memoization; independent external wire vectors. | Broader malformed-event corpus and cross-client traces are verification limits. |
| [CORD-02](https://github.com/concord-protocol/concord/blob/b84554ea5dd47510057a580fa2f8587b4399ad17/02.md) communities | Split-control genesis/read/write, legacy read, exact root history, opaque metadata, self-encrypted 33302 fragmentation, bounded missing-fragment recovery, partial-list scoped writes, signed-wire strict-shrink repairs, lossless u64 numeric boundaries, authenticated relay migration, dissolution discovery and sealed-history enforcement. | Unsafe numeric values require native lossless JSON support; older engines fail closed. Broad cross-client fragment/recovery traces remain a verification limit. |
| [CORD-03](https://github.com/concord-protocol/concord/blob/b84554ea5dd47510057a580fa2f8587b4399ad17/03.md) channels | Public/private read with held keys; owner private creation with independent keys, scoped Role/Grants, durable key backup and proof-checked minimal direct invitations; channel rekey/conversion history, retained authenticated history provenance across compaction and terminal deletion; messages/replies/reactions/deletes/edits. | Existing-channel conversion and delegated private-channel management UI remain limited. Prior-public history without a private key is read-only. |
| [CORD-04](https://github.com/concord-protocol/concord/blob/b84554ea5dd47510057a580fa2f8587b4399ad17/04.md) roles | Edition/rank/citation folding, bans/moderation, delegated settings writers with current Grant proofs, creator-bound invite registry writer, revoked-author rejection, authenticated 40-byte staff key adoption, scoped Pin Lists with disclosure verification and compaction, and queued self-delete reconciliation. | General role and pin-management UI are incomplete; wire inspection/writer APIs are covered. Automatic curator edit refresh remains a SHOULD-level followup; received edits are checked against the same target rules as the timeline. |
| [CORD-05](https://github.com/concord-protocol/concord/blob/b84554ea5dd47510057a580fa2f8587b4399ad17/05.md) invites | Public 33301 links, standard NIP-59 direct 3313 invitations/inbox, expiry preview versus acceptance, same-root private-key grants, encrypted 13303 creator list, serialized create/refresh/retire management, signed tombstones, original-link registration, final-link durable refounding prerequisites and owner-control-root backup before genesis publication. | Interrupted publication may still need completion of partial relay writes. Owner keys and signed refounding prerequisites are retained. Cross-client relay interoperability remains unverified. |
| [CORD-06](https://github.com/concord-protocol/concord/blob/b84554ea5dd47510057a580fa2f8587b4399ad17/06.md) rekeys | 3303 discovery/decryption/application; 72/104/136-byte payloads and commitments; retained roots/private keys; explicit recipient review/exclusion; base split upgrade; refounding plan/checkpoint/resume; authenticated compacted snapshots; dissolution and sealed-history admission. | General recipient-management UX is limited; the explicit reviewed-recipient workflow is available. External signer adapters must support the required binary NIP-44 operation. |
| [CORD-07](https://github.com/concord-protocol/concord/blob/b84554ea5dd47510057a580fa2f8587b4399ad17/07.md) A/V | Blind-broker token flow, independent sender-key derivation, signed ephemeral presence/rendezvous, per-sender LiveKit frame encryption, authenticated media attachment, occupied-broker priority, generation-bound migration/rotation, mic/camera/screen sharing and capture cleanup. | No live cross-client broker/SFU call has been validated. This is a verification limit, not a demonstrated wire defect. |
| [CORD-08](https://github.com/concord-protocol/concord/blob/b84554ea5dd47510057a580fa2f8587b4399ad17/08.md) expiry | Signed expiration tagging, receive/display enforcement, memo invalidation, authenticated ciphertext/pending-send purge, exempt control/delete/notice events, timer settings and accepted-metadata-first 1740 notice fanout. | Notice delivery can fail after accepted metadata; the UI reports it. Live relay interoperability remains unverified. |

## Resolved audit findings

The source-confirmed gaps in the initial matrix received implementation changes
and regression coverage:

1. **Staff delivery and pins (CORD-04 §§3,7).** Staff Grants carry the authenticated
   40-byte epoch/control-root capsule. Adoption rechecks the current Grant after
   decryption and remains account-bound. Pin Lists preserve edition chains when
   content exceeds limits; proof verification binds author, channel, signed
   ciphertext and disclosure keys. Tests cover unreadable private epochs, role
   scope, foreign/multi-target edits, self-deletes, stale heads and queued retries.
2. **Authoritative relays (CORD-02 §6).** Metadata routing supersedes invite
   bootstrap routing, including same-pass hydration, live subscriptions and
   legacy rooms cached by invite address. Authenticated empty relay sets do not
   silently restore obsolete defaults.
3. **Oversized-fragment repair (CORD-02 §8).** Strict shrink compares complete
   encrypted and signed event sizes. A partial-list leave can shrink an oversized
   coordinate without repacking unseen fragments; unchanged ciphertext padding
   does not qualify just because plaintext became shorter.
4. **Numeric wire values (CORD-02 §8 / CORD-05).** Membership, public/direct
   invitations and saved state preserve full-range integer values and their
   numeric wire type. Timestamp merges do not round adjacent u64 values together.
   Exact source-token checks reject fractional epochs that JavaScript would round
   to integers and preserve precise opaque numbers. Native browser coverage and
   independent encrypted vectors exercise these boundaries.
5. **Channel lifecycle (CORD-03 §2).** Visibility conversions retain streams whose
   secrets the member holds. Authenticated prior-public provenance is retained in
   self-membership/rekey state before compaction and stripped at external invite
   acceptance. Deleted channels cannot reappear through later descendants or the
   private-key fallback list; malformed/orphan deletion candidates do not erase a
   valid live channel.
6. **Creation recovery.** The creator's owner membership/control root is backed
   up before public genesis publication, alongside creator-link bookkeeping.
   Late account changes cannot publish another account's announcement or return
   an old-account room into the current session.

These findings have bounded tests, not an exhaustive proof that every possible
protocol input or interleaving is covered. Newly demonstrated failures should be
added here with their reproduction and regression test.

## Optional UI and validation limits

General role/promotion editing, existing-channel private grant editing, pin
management/first-list initialization and conversion management are incomplete
product workflows. They do not excuse
required receive/fold behavior listed above. Interrupted private creation keeps
its key but may require manual completion of partially published access records.

New-community creation backs up both owner membership and the original link
signer before genesis publication. Partial relay publication can still need
retry/recovery; a passing acknowledgement cannot guarantee every third-party
relay retains an event indefinitely.

Full-range numeric values require native `JSON.rawJSON` and JSON parse source
support. Older engines refuse out-of-safe-integer-range values rather than round them.
Binary staff/rekey delivery similarly requires signer-adapter support; unsupported
external signers fail closed. No universal NIP-46/bunker compatibility is claimed.

Wire vectors, real cryptography and browser lifecycle tests do not replace a
live cross-client exercise. No live CORD-07 SFU call or broad malformed-event
fuzz corpus is claimed. Automatic timer notices are best effort after accepted
metadata and failures are surfaced.

## Final review record

The final gap-closure review covered these source commits (integration may
cherry-pick them under new hashes):

- `2a6c87f62`, `07738d115`, `ba7c5fa9b`: strict shrink and exact numeric boundaries.
- `525629d38`, `cc76a1426`: staff delivery, Pin Lists and queued reconciliation.
- `75e786fb1`: relay migration, channel lifecycle and retained conversion history.
- `810c9049e`: owner-key backup before genesis publication.
- `267549b6a`: refuse calls when only prior channel history remains readable.

Independent reruns passed the staff/pin/rekey runtime and the signed
metadata/channel lifecycle runtime. Together they include refounder and recipient
history recovery from compacted-only state after serialized reload. The author
reported 81 passing combined lifecycle/UI/browser tests; the staff/pin broad run
reported 348 passing tests. These are separate worktree results with overlapping
coverage, not a combined release-gate total. Four focused numeric tests, including
native Chrome, passed after the fractional-token followup. An independent numeric-parser mutation failed; the lifecycle author reported
four additional rejected mutations.

No concrete MUST-level blocker remains from the findings recorded in this audit.
The integrator must still run the combined gate after resolving overlapping
changes. This record does not claim exhaustive protocol certification or a
successful live cross-client call.

## Independent regression test

`tests/client/cord_spec_compat_runtime.mjs` constructs wire events independently
of the shipped reader/writer: Node's HKDF-SHA256 and SHA-256 implement the frozen
CORD-02 byte layouts, and NostrTools constructs/signs/encrypts the event layers.
This avoids a writer and reader agreeing on the same wrong derivation. No relay,
live membership, or secret user data is involved.

Assertions cover split-control public address and read key separation, staff
signature correctness, member read-only capability, mismatched keys, legacy
reading, malformed envelope reuse after valid memo entries, different read keys
at one claimed address, current-epoch writes with archived roots, and verbatim
unknown metadata values. External chat wraps are independently produced too.

The browser transport propagates `null` for a valid split-control read-only
capability. It must not attempt to sign as the staff stream. A relay demanding
proof of that particular signer cannot be satisfied by an ordinary reader;
using the community read key as the signer would create the wrong identity.

## Release interpretation

The combined core/membership/expiry/private/rekey suite passed **309 tests** in
its integration worktree. The final voice suite, including screen-share capture
races, passed **18 tests**. Creator lifecycle tests use real signatures and
cover signed-list races, explicit relay acknowledgements, concurrent operations,
account changes and durable final-link retirement. These suites overlap other
reported focused runs; their counts must not be added as unique coverage.

Independent final review reran `test_cord_invite_links.mjs` after the default
creation announcement fix (`118fb5340`): all eight runtime groups passed. The
actual adapter refounding runtime also passed with a failed prerequisite relay
acknowledgement, serialized checkpoint reload and plain resume. The final
combined release gate remains the integrator's responsibility. Passing these
paths does **not** certify universal protocol interoperability; the external
verification and product limitations above still apply.

## Membership writer validation

`concord_membership_fragments_runtime.mjs` uses actual NIP-44 self-encryption and
Schnorr signatures. It verifies current wire kind, named base64url keys, seed
omission/cosmetics, opaque snapshot/entry/channel/tombstone/fragment fields,
large-list fragmentation measured after signing, partial-list leave, refusal to
repack an incomplete list, same-second coordinate monotonicity, queued updates
against stale relay reads, unreadable latest fragments, account changes, and a
newer fragment observed while a signer is open. New overflow fragments publish
before replacing their source; fragment zero receives unknown top-level fields
before their old locations are cleared. Cross-device leave/rejoin runs against
the new writer. The related suite passed 81 cases. Four removed-guard mutations
failed (wire-size limit, retired kind, monotonic timestamp, seed omission).

## Split genesis and invite validation

`cord_invite_genesis_runtime.mjs` verifies the public writer against an independent
HKDF control-signer derivation and independently decrypts the invite bundle.
It checks that member invites omit the staff root, owner membership can sign the
control address, the genesis contains exactly metadata and general-channel
editions, and legacy readers remain covered by the earlier independent fixture.
Signed newest-coordinate revocation, wrong-coordinate and unsigned decoys,
expired preview versus acceptance, 256-channel/5-relay bounds, opaque fields,
UTF-8 name/description caps and incoming oversized NIP-44 plaintext are covered.
The lenient bundled cipher is exposed only inside this test to construct the
oversized negative input; normal vectors use independent NostrTools encryption.

`PosterCordReader.validateInviteBundle(input, {forJoin, now})` is the shared pure
validator. It does no networking and returns copied normalized join material.
Wire invites use lowercase hex; base64url conversion belongs exclusively to the
Community List boundary. Epoch inputs normalize to safe integers or exact decimal u64 strings internally; wire parsing and serialization preserve numeric values through lossless JSON. Membership comparisons use BigInt. Already-rounded unsafe JavaScript inputs are rejected. Public/direct invitations, membership fragments and browser storage have independent boundary tests.

## Fragment recovery validation

`concord_membership_recovery_runtime.mjs` uses signed self-encrypted input for a
100-fragment list despite an initial 64-event page. Recovery fetches exact
missing coordinates in bounded batches, remembers ciphertext heads, crosses
holes and later fills them, and isolates account identities. Forged and
wrong-author events cannot contribute fragment state. An enormous declared
count neither allocates a matching array nor starts an unbounded query loop.
Foreground writes may recover several batches; incomplete lists remain safe to
read and support scoped edits, but cannot repack until complete.

## Integrated expiry and delegated settings validation

The integration includes the CORD-08 core implementation and real IndexedDB
expiry tests. Timer runtime fixtures sign and decrypt actual metadata/notices,
cover every held channel, unchanged profile edits, metadata rejection, partial
notice delivery, and account changes during signing. Delegated staff fixtures
verify exact Grant citations and revoked-author rejection in both writers and
folding. The settings visibility selector is disabled: the old local flag did
not rotate keys or create private membership grants and falsely claimed privacy.
Owner private creation now uses the verified role-grant and encrypted-invite path;
existing-channel conversions remain unavailable. Open settings are protected from background repaint so input
changes survive incoming history refreshes.

## Private-channel first-access validation

Actual encrypted runtime fixtures prove that a public bundle cannot derive the
new channel key, the granted recipient can decrypt its messages, missing role
proof and banned recipients cannot obtain a key-bearing invitation, and staff,
archived and unknown vault secrets are omitted. Adapter tests verify that key
backup precedes channel publication and rejected access records release no key.
Interrupted creation retains the key for recovery; completing or cleaning up
partially published access records is still a manual recovery limitation.
