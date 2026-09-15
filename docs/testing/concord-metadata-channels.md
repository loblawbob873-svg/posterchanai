# Authenticated metadata migration and channel history

The invite relay list bootstraps a community. Verified metadata replaces it for subsequent reads, publications and live subscriptions. The current hydration pass updates its captured targets immediately; legacy rooms indexed by invite address still resolve the same verified control cache. Freshly created rooms retain their canonical community ID.

Channel history uses each public root and private key the member actually holds. Public-to-private conversion leaves prior public history readable without granting access to current private messages. Such members receive a history-only view and cannot send. Private-to-public conversion preserves held private history while new members can read only public streams.

Deletion is terminal along an authenticated edition chain. A descendant cannot unset deletion, retained private keys cannot restore the deleted channel, and an empty final channel list does not synthesize a new general channel. Unrelated or unauthorized tombstones cannot remove the selected chain.

Authenticated prior-public provenance is retained as internal `public_channel_history` in the self-encrypted membership. Normal metadata synchronization, membership writing, refounding preparation and received rekey adoption preserve it. External invite acceptance removes the field; shareable/direct exports omit it. This keeps history readable after a restart with only compacted control heads.

Run `tests/client/test_concord_metadata_channels.py`, `test_concord_rekey.py`, and `test_concord_ui.py`. Independent producer vectors cover conversion epochs, key boundaries, authoritative relays, terminal ancestry, compaction and untrusted provenance. The shipped controller harness checks same-pass hydration and live relay migration. Recipient rekey coverage uses actual encrypted3303 input and a compacted-only reload. Mutation checks fail when metadata adoption, terminal deletion, recipient provenance, or same-pass routing is removed.
