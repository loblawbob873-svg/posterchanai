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
production deployment. Remaining normative gaps below prevent a claim of full
CORD-01 through CORD-08 compatibility.

| Spec | Implemented and covered by regression tests | Remaining work |
| --- | --- | --- |
| [CORD-01](https://github.com/concord-protocol/concord/blob/b84554ea5dd47510057a580fa2f8587b4399ad17/01.md) streams | NIP-44 self-ECDH, signed persistent/ephemeral wraps, encrypted/plaintext seals, rumor hashes, author/channel binding, layer-size caps, wire/capability-scoped memoization; independent external wire vectors. | Broader malformed-event corpus and cross-client traces are verification limits. |
| [CORD-02](https://github.com/concord-protocol/concord/blob/b84554ea5dd47510057a580fa2f8587b4399ad17/02.md) communities | Split-control genesis/read/write, legacy read, exact root history, opaque metadata, self-encrypted 33302 fragmentation, bounded missing-fragment recovery, partial-list scoped writes, dissolution discovery and sealed-history enforcement. | Authoritative relay migration, strict-shrink exception and lossless full-u64 JSON encoding: gaps 3–5 below. |
| [CORD-03](https://github.com/concord-protocol/concord/blob/b84554ea5dd47510057a580fa2f8587b4399ad17/03.md) channels | Public/private read with held keys; owner private creation with independent keys, scoped Role/Grants, durable key backup and proof-checked minimal direct invitations; channel rekey history; messages/replies/reactions/deletes/edits. | Receiving visibility changes must preserve all readable prior streams; terminal deletion needs explicit enforcement/negative vectors. Existing-channel conversion UI remains disabled. See gap 6. |
| [CORD-04](https://github.com/concord-protocol/concord/blob/b84554ea5dd47510057a580fa2f8587b4399ad17/04.md) roles | Edition/rank/citation folding, bans/moderation, delegated settings writers with current Grant proofs, creator-bound invite registry writer, revoked-author rejection. | Staff promotion key capsules and canonical Pin List folding/compaction are gaps 1–2. General role editing is additionally a product UI limitation. |
| [CORD-05](https://github.com/concord-protocol/concord/blob/b84554ea5dd47510057a580fa2f8587b4399ad17/05.md) invites | Public 33301 links, standard NIP-59 direct 3313 invitations/inbox, expiry preview versus acceptance, same-root private-key grants, encrypted 13303 creator list, serialized create/refresh/retire management, signed tombstones, original-link registration, final-link durable refounding prerequisites. | Interrupted new-community creation can retain creator-link recovery material without having saved the owner membership/control root; see durability limitation below. Cross-client relay interoperability remains unverified. |
| [CORD-06](https://github.com/concord-protocol/concord/blob/b84554ea5dd47510057a580fa2f8587b4399ad17/06.md) rekeys | 3303 discovery/decryption/application; 72/104/136-byte payloads and commitments; retained roots/private keys; explicit recipient review/exclusion; base split upgrade; refounding plan/checkpoint/resume; authenticated compacted snapshots; dissolution and sealed-history admission. | Valid Pin List editions currently cause safe refusal of unsupported compaction, rather than complete refounding support. General recipient-management UX is limited; the explicit reviewed-recipient workflow is available. |
| [CORD-07](https://github.com/concord-protocol/concord/blob/b84554ea5dd47510057a580fa2f8587b4399ad17/07.md) A/V | Blind-broker token flow, independent sender-key derivation, signed ephemeral presence/rendezvous, per-sender LiveKit frame encryption, authenticated media attachment, occupied-broker priority, generation-bound migration/rotation, mic/camera/screen sharing and capture cleanup. | No live cross-client broker/SFU call has been validated. This is a verification limit, not a demonstrated wire defect. |
| [CORD-08](https://github.com/concord-protocol/concord/blob/b84554ea5dd47510057a580fa2f8587b4399ad17/08.md) expiry | Signed expiration tagging, receive/display enforcement, memo invalidation, authenticated ciphertext/pending-send purge, exempt control/delete/notice events, timer settings and accepted-metadata-first 1740 notice fanout. | Notice delivery can fail after accepted metadata; the UI reports it. Live relay interoperability remains unverified. |

## Remaining normative interoperability gaps

These are specific source-review findings against the pinned specification.
They are not waived by the passing tests. A finding without an adversarial
runtime vector still needs that vector before it can be marked resolved.

1. **Staff promotion key delivery (CORD-04 §3).** A first staff-making Grant
   must carry the granter-to-recipient encrypted 40-byte `control_wrap`
   (`epoch_be8 || control_root32`); the recipient must check the derived
   control signer. There is no receiving/adoption implementation for this
   capsule. Existing 72/104/136-byte rekey handling does not substitute for it.
   Permission folding alone cannot give newly promoted staff the signing key.
2. **Pin List (CORD-04 §11).** `PIN_MESSAGES` bit 11 and vsk 11 editions are not
   implemented throughout permission/fold/refounding handling. Valid proof
   bundles need channel/author validation; invalid or oversized content must
   fold as an empty list without breaking its edition chain. Current refounding
   safely refuses unsupported editions. Missing a pin button is optional UI;
   missing received pin state and compaction is a protocol gap.
3. **Authoritative relay updates (CORD-02 §6).** Metadata relay lists are folded,
   but inspection/subscription still uses the invite/membership relay snapshot.
   A valid metadata migration must update the authoritative relay set while
   retaining bootstrap behavior needed to obtain the first fold.
4. **Strictly smaller oversized fragments (CORD-02 §8).** The membership writer
   always applies its local event-size ceiling. It lacks the required exception
   for a strictly smaller rewrite of an already oversized fragment, needed for
   leave/repair. This must compare actual encoded event sizes; relay rejection
   remains possible even when local validation permits the shrink.
5. **Entire u64 range on JSON wire (CORD-02 §8).** Internal comparisons preserve
   decimal u64 values, but unsafe JavaScript numeric inputs are rejected and
   exact large internal strings serialize quoted. The normative numeric wire
   type requires a lossless JSON parser/serializer. Ordinary safe-integer epochs
   are covered; full-range interoperability is not.
6. **Visibility-transition history and deletion (CORD-03 §2).** Channel views
   choose either held public-root streams or held private-key streams based on
   current visibility. They do not combine both histories after a valid peer
   conversion, despite retained decryption material. The latest-metadata fold
   also needs an adversarial test and explicit rule preventing resurrection
   after a terminal channel deletion. Disabling our conversion control avoids
   publishing an unsafe local flag change but does not resolve received events.

## Optional UI and validation limits

General role/promotion editing, existing-channel private grant editing and
conversion management are incomplete product workflows. They do not excuse
required receive/fold behavior listed above. Interrupted private creation keeps
its key but may require manual completion of partially published access records.

New-community creation backs up the original link signer in encrypted 13303
before publishing genesis and keeps announcements bound to the captured account.
The owner membership, including its independent control root, is saved after
successful creation. A late failure or page loss before that save can therefore
leave incomplete owner recovery; creator-link backup alone is insufficient.

Wire vectors, real cryptography and browser lifecycle tests do not replace a
live cross-client exercise. No live CORD-07 SFU call or broad malformed-event
fuzz corpus is claimed. Automatic timer notices are best effort after accepted
metadata and failures are surfaced.

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
paths does **not** establish full protocol support while the gaps above remain.

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
Community List boundary. Epoch inputs accept safe JavaScript integers or exact canonical decimal u64 strings from rekey processing; unsafe numeric input is rejected rather than rounded. Membership comparisons use BigInt. Emitting normative unquoted JSON numbers across the entire u64 range still requires a lossless JSON codec.

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
