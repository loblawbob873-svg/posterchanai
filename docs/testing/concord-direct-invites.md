# CORD-05 direct invitations

Implements section6 of concord-protocol/concord revision
`b84554ea5dd47510057a580fa2f8587b4399ad17`.

- Standard NIP-59 outer1059, verified kind13 seal, authenticated unsigned3313 rumor.
- Index hint `k=3313` and optional NIP-40 expiration are signed into ephemeral outer events.
- Dedicated recipient-indexed inbox plus acceptance of untagged3313 traffic discovered by general giftwrap ingestion.
- Delivery uses existing recipient10050 discovery, with10002 read-relay fallback, including inboxes already in the connected relay pool. Success requires a relay acknowledgement.
- Preview validates owner commitment and bundle bounds, allows expired previews, and refuses expired acceptance. Preview shows text only: supplied icons and community relays are untouched until acceptance.
- Pending storage is per account, at most32 encrypted giftwraps and3MiB, with200 bounded dismissed IDs. No plaintext community keys persist in this inbox.
- Explicit acceptance persists33302 membership even without a link. Same-root private grants merge channel keys without dropping existing channels or staff material. Older channel epochs and equal-epoch key conflicts are rejected. Higher channel epochs preserve prior held keys.
- An invitation cannot replace the root/control signer of an already joined community: authenticated3303 rekey processing must establish that change first.
- Sending includes only current invite fields/channel capabilities; internal provenance, archives and staff roots are excluded. Account and optional caller authorization guards survive signer/discovery awaits.

The direct path never creates a link, writes the creator13303 Invite List, or publishes a registry listing. Those link-management operations remain a separate implementation.

Tests: independent NIP-59 producer plus actual worker (`test_cord_direct_invites`), shipped app transport/ingest functions (`test_cord_direct_transport`), actual encrypted33302 persistence and grant merge (`test_cord_direct_membership`), and real bundled Chrome at1280/390 widths (`test_cord_direct_invites_full_app`). Browser tests cover review, decline, escaped names/no icon, absence of premature community queries/membership, and username autocomplete selecting an npub without sending until the Send button is pressed.
