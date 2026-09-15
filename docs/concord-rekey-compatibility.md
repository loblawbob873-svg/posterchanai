# CORD06 implementation boundary

Reviewed against Concord commit `b84554ea5dd47510057a580fa2f8587b4399ad17`,
`06.md`, `02.md` Appendix A, and `examples.md` (one-based chunk indexes).

Implemented and exercised with independent Node cryptography vectors:

- Authenticated 1059 / encrypted 20013 / 3303 transport, base and private-channel
  addresses, pairwise recipient locators and epoch-key commitments.
- Exact 72/104/136-byte blobs, scope/epoch binding, staff signing-key validation.
  New base writers only produce split 104/136-byte forms; legacy base72 is read-only.
- Current roster authority and grant citations, rank checks for exclusions,
  complete chunk-set removal, rejected corrupt addressed blobs, and separate
  sets for competing rotators.
- Downward same-epoch root convergence; retained roots and private-channel keys
  keep history readable. Per-channel `held_keys` is a preserved vault extension.
- Account-owned subscriptions, local membership adoption, subsequent encrypted
  membership persistence, and read-only writes after confirmed removal.
- Local signer worker byte encryption/decryption; no private or conversation key
  leaves the worker. Unsupported signers fail explicitly.

## Remaining interoperability constraints

CORD06 calls its plaintext arbitrary fixed-width bytes. NIP44's standard string
API UTF-8 encodes/decodes plaintext, while NIP46 returns a JSON string. Arbitrary
key bytes cannot round-trip through those APIs. This implementation uses the
literal CORD06 binary layout inside NIP44's authenticated framing for local keys;
it does **not** invent an extra base64/hex plaintext convention for remote signers.
Upstream must specify a lossless NIP46 representation before remote signer support
can be claimed. See <https://github.com/nostr-protocol/nips/blob/master/44.md>.

The refounding coordinator now acquires every held control plane from every selected
relay before writing, refuses incomplete or unsupported control state, compacts
verified heads without changing their authors' signed seals, and persists a
resumable plan before publication. It confirms all root chunks before compacted
control and private-channel rotations, retries the exact signed events after an
interruption, and treats the final guestbook snapshot as best effort. Channel
rotations use the prior base root. Fresh joiners bootstrap compacted current-epoch
heads without requiring discarded predecessor history.

Owner settings expose key rotation and resumption. Ban uses the same coordinator,
including the signed ban in the new control state. Both require explicit review of
retained community and private-channel recipients: the observed member list is
not a complete inventory of private-key holders. Missing lists abort before any
publication. The receiver records an authenticated epoch minter for guestbook
snapshots; external invitations cannot nominate that internal provenance field.

Epoch arithmetic uses unsigned 64-bit integers and decimal strings in adopted
local material. JSON numeric wire snapshots above JavaScript's safe integer range
need a lossless JSON serializer/parser; ordinary JSON number coercion is not safe.

## Dissolution

The reader/writer validates the owner-signed chainless CORD02 tombstone at the
public, epoch-independent address. The signed `eid` must equal this community;
zero placeholders and rewrapped tombstones from the same owner's other community
are rejected. Receipt persists a terminal marker, stops live chat/rekey and
metadata refresh, retains historical keys, and blocks future writes/rekeys.

At dissolution, the adapter records previously cached encrypted wrap identities
locally. Subsequent reads admit that frozen history and verified self-deletes;
new messages, edits and reactions are rejected even if backdated. Moderation
cannot delete another author's history after sealing. Own-message deletion remains
writable, and live chat/rekey/control refresh stops. Manually reopening history
can retrieve and apply later self-deletes.

Unseen old messages cannot be distinguished cryptographically from newly authored,
backdated messages with this wire format. The sealed view therefore admits only
history already cached before sealing, retaining the keys and ciphertext. It does
not pretend an author-controlled timestamp establishes historical authenticity.

Final invitation retirement can attach signed, chainless kind-33301 tombstones and
explicit bootstrap relays as durable refounding prerequisites. The complete plan
is validated and saved first; every listed bootstrap relay must acknowledge each
retirement before any root event is published. A normal resumed operation retains
those exact signed events and destinations, including after partial relay success.
Unavailable bootstrap relays leave a visible resumable operation; they cannot be
silently skipped by an aggregate acknowledgement from a different relay.
