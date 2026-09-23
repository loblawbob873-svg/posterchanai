"""ActivityPub server for PosterChan, with Nostr as its only store.

Every local user this node gave a NIP-05 name is, automatically, an ActivityPub actor at
`https://<domain>/ap/users/<name>`, discoverable as `@name@<domain>` (see actors.py).

NOTHING HERE HAS A TABLE OF POSTS. It is the existing fediverse bridge's model, extended:

  * IN:  an activity that reaches our inbox becomes an ordinary Nostr event signed by the same
         per-author PUPPET key the Pleroma timeline bridge uses (fedi_bridge_identity), tagged with a
         NIP-48 `proxy` back to the ActivityPub object, and recorded in the SAME `FediBridgeDelivered`
         dedup table -- so a note that arrives both ways is one Nostr event, never two.
  * OUT: a member's own Nostr events are READ from the local relay and translated on the way out
         (`/ap/objects/<event id>` is served straight from the relay).
  * State that is neither (followers, the per-member RSA keys ActivityPub needs, the delivery
         cursor) lives in operator-signed, encrypted kind-30078 documents on the node's own relay.

Modules: config (settings), convert (pure Nostr<->AP translation), httpsig (HTTP Signatures),
keys + state (the documents), remote (SSRF-safe fetch of remote actors/objects), actors (our side),
inbox (incoming), outbox (outgoing, run in the worker). Routes: app/routers/activitypub.py.
"""
