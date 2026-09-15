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

The low-level writer prepares rekey events but is not a complete refounding
coordinator. Reliable control-history acquisition, verified compacted-head
publication after root acceptance, resumable multi-channel rotation, and
post-refounding guestbook seeding require a separate implementation. Existing
ban controls must not be described as providing cryptographic exclusion merely
because they publish a banlist.

Epoch arithmetic uses unsigned 64-bit integers and decimal strings in adopted
local material. JSON numeric wire snapshots above JavaScript's safe integer range
need a lossless JSON serializer/parser; ordinary JSON number coercion is not safe.
