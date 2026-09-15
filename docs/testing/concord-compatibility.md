# Concord compatibility audit

Audited 2026-09-15 against the normative [Concord specifications at
b84554ea5dd47510057a580fa2f8587b4399ad17](https://github.com/concord-protocol/concord/tree/b84554ea5dd47510057a580fa2f8587b4399ad17).
The upstream repository describes an evolving protocol. Compatibility must be
qualified by this revision; passing our own round-trip tests does not establish
interoperability with every revision or client.

## Coverage and gaps

| Spec | Implemented / verified | Remaining compatibility work at this audit |
| --- | --- | --- |
| [CORD-01](https://github.com/concord-protocol/concord/blob/b84554ea5dd47510057a580fa2f8587b4399ad17/01.md) streams | NIP-44 self-ECDH, signed 1059/21059 wraps, encrypted 20013 and plaintext 20014 seals, rumor hashes and author matching, channel binding, plaintext publishing cap. This patch verifies outer signatures and scopes memo verdicts to wire bytes and read capability. | Broader independent malformed-event/encoding/receive-size corpus. No claim that every library acceptance rule exactly matches normative encoding. |
| [CORD-02](https://github.com/concord-protocol/concord/blob/b84554ea5dd47510057a580fa2f8587b4399ad17/02.md) communities | Self-certifying owner commitment, legacy control, chat and guestbook planes, member coalescing, unknown metadata preservation. This patch adds split control read/write key support and current-epoch writes. | New-community writer now emits split-control genesis and a member invite containing only its signer pubkey; owner membership retains the staff root. Membership writer now emits canonical self-encrypted kind 33302 fragments; actual signed-event size limits, partial writes, opaque fields, monotonic timestamps and interrupted/stale/account-changing writes have independent runtime coverage. Exact-coordinate bounded recovery now continues past the initial relay page and tolerates holes/late arrivals. Dissolution tombstone discovery/enforcement absent. Snapshot authority and all removed-member/refounding cases require independent audit. |
| [CORD-03](https://github.com/concord-protocol/concord/blob/b84554ea5dd47510057a580fa2f8587b4399ad17/03.md) channels | Public/private channel reading when keys held, public creation, signed channel/epoch binding, messages/replies/reactions/deletes/edits. Independent external chat event verified by new test. | Private creation/grant delivery intentionally unavailable. Private historical `priors` read handling and removal/rekey convergence need completion. |
| [CORD-04](https://github.com/concord-protocol/concord/blob/b84554ea5dd47510057a580fa2f8587b4399ad17/04.md) roles | Edition chain folding, role/grant ranks, citations, owner and ranked moderator deletion, bans. Actual-crypto regression tests cover grants and moderation. Delegated metadata/channel writers now cite the exact current Grant and reject revoked/banned staff. | Role/grant management UI/write paths restricted; staff `control_root` grant distribution, canonical pin-list vsk 11 proof bundles, and full independent fork/rehydration/hard-cap vectors still need implementation or verification. |
| [CORD-05](https://github.com/concord-protocol/concord/blob/b84554ea5dd47510057a580fa2f8587b4399ad17/05.md) invites | Public 33301 bundle links, v4 relay dictionary, invite preview/accept separation, join attribution. | Direct 3313 NIP-59 invites and Invite List 13303 sync not implemented in public writer API. Independent tests now cover expiry preview versus acceptance, signed newest-coordinate revocation without rollback, owner commitment, channel/relay bounds and signer-secret retention. Link refresh after rekey remains. |
| [CORD-06](https://github.com/concord-protocol/concord/blob/b84554ea5dd47510057a580fa2f8587b4399ad17/06.md) rekeys | Reading explicitly held root epochs and public-channel history; retained legacy plane compatibility. | No adapter 3303 rekey discovery/decryption/application flow found. Mandatory 72/104/136-byte blob forms, commitment verification, recipient exclusion, base split upgrade, and refounding compaction must be implemented/tested. A ban alone does not rotate secrets. |
| [CORD-07](https://github.com/concord-protocol/concord/blob/b84554ea5dd47510057a580fa2f8587b4399ad17/07.md) A/V | Voice signer and media root derivations exist inside bundle. | No interoperable blind broker token flow, per-sender frame keys, signed presence/rendezvous implementation found. Existing application calls are not evidence of CORD-07 support. |
| [CORD-08](https://github.com/concord-protocol/concord/blob/b84554ea5dd47510057a580fa2f8587b4399ad17/08.md) expiry | Signed expiration tagging, receive/display enforcement, memo invalidation, authenticated ciphertext/pending-send purge, exempt control/delete/notice events, timer settings, and accepted-metadata-first kind1740 fanout to held channels. | Notice delivery can fail independently of accepted metadata and is reported. Live relay interoperability is not established by local fixtures. |

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

Core and bot regression tests passed before edits (29 cases); the expanded
focused suite passed after edits (47 cases). These results establish the listed
paths, **not full CORD-01 through CORD-08 support**. Remaining rows are concrete
work, not waived requirements. Keep this matrix alongside subsequent feature
patches so deployment reports do not confuse legacy compatibility with complete
current-protocol compatibility.

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
Private-channel creation/conversion remains unavailable until key distribution
is implemented. Open settings are protected from background repaint so input
changes survive incoming history refreshes.
