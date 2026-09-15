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
| [CORD-02](https://github.com/concord-protocol/concord/blob/b84554ea5dd47510057a580fa2f8587b4399ad17/02.md) communities | Self-certifying owner commitment, legacy control, chat and guestbook planes, member coalescing, unknown metadata preservation. This patch adds split control read/write key support and current-epoch writes. | New-community writer still creates legacy rather than split-control genesis. Membership writer now emits canonical self-encrypted kind 33302 fragments; actual signed-event size limits, partial writes, opaque fields, monotonic timestamps and interrupted/stale/account-changing writes have independent runtime coverage. Large-list recovery beyond the initial relay query page remains to be expanded. Dissolution tombstone discovery/enforcement absent. Snapshot authority and all removed-member/refounding cases require independent audit. |
| [CORD-03](https://github.com/concord-protocol/concord/blob/b84554ea5dd47510057a580fa2f8587b4399ad17/03.md) channels | Public/private channel reading when keys held, public creation, signed channel/epoch binding, messages/replies/reactions/deletes/edits. Independent external chat event verified by new test. | Private creation/grant delivery intentionally unavailable. Private historical `priors` read handling and removal/rekey convergence need completion. |
| [CORD-04](https://github.com/concord-protocol/concord/blob/b84554ea5dd47510057a580fa2f8587b4399ad17/04.md) roles | Edition chain folding, role/grant ranks, citations, owner and ranked moderator deletion, bans. Existing actual-crypto regression tests cover grants and moderation. | Role/grant management UI/write paths restricted; staff `control_root` grant distribution, canonical pin-list vsk 11 proof bundles, and full independent fork/rehydration/hard-cap vectors still need implementation or verification. |
| [CORD-05](https://github.com/concord-protocol/concord/blob/b84554ea5dd47510057a580fa2f8587b4399ad17/05.md) invites | Public 33301 bundle links, v4 relay dictionary, invite preview/accept separation, join attribution. | Direct 3313 NIP-59 invites and Invite List 13303 sync not implemented in public writer API. Independently verify expiry/revocation, link refresh after rekey, channel allocation bounds, and signer-secret retention. |
| [CORD-06](https://github.com/concord-protocol/concord/blob/b84554ea5dd47510057a580fa2f8587b4399ad17/06.md) rekeys | Reading explicitly held root epochs and public-channel history; retained legacy plane compatibility. | No adapter 3303 rekey discovery/decryption/application flow found. Mandatory 72/104/136-byte blob forms, commitment verification, recipient exclusion, base split upgrade, and refounding compaction must be implemented/tested. A ban alone does not rotate secrets. |
| [CORD-07](https://github.com/concord-protocol/concord/blob/b84554ea5dd47510057a580fa2f8587b4399ad17/07.md) A/V | Voice signer and media root derivations exist inside bundle. | No interoperable blind broker token flow, per-sender frame keys, signed presence/rendezvous implementation found. Existing application calls are not evidence of CORD-07 support. |
| [CORD-08](https://github.com/concord-protocol/concord/blob/b84554ea5dd47510057a580fa2f8587b4399ad17/08.md) expiry | This patch preserves `message_expiration` and opaque future metadata during rename. | At audit baseline no timer tagging, expiry ingest/display/purge enforcement, timer notices, or setting UI. Separate implementation in progress; update this row only with integrated test evidence. |

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
