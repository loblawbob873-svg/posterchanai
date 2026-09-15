# CORD-05 creator link lifecycle

The creator link manager uses signed, self-encrypted kind 13303 for token/signing-key bookkeeping and authenticated control subkind 8 for public link coordinates. Direct invitations do not enter either list. Existing URL copy remains a one-click action; the separate Links action manages creator links. An original locally held link can be explicitly recovered into bookkeeping.

Minting saves the encrypted signing key before publishing kind 33301, then publishes the creator-bound registry edition. Retirement publishes the signed kind 33301 tombstone before removing the registry coordinate and recording the terminal creator-list tombstone. Retiring the final link requires the refounding implementation's `refoundingBeforeEvents` capability: the signed tombstone and relay destinations enter its durable prepared plan and precede root publication.

Creator list writes require complete relay responses and re-read after asynchronous signing to detect another device's changes. Whole lifecycle operations serialize per account/community. Refreshes use the current verified creator registry and captured control generation, never revive a signed tombstone, and never add private-channel keys to a public link. Incomplete control/list/link reads fail closed; bounded full control pages require a later pagination improvement rather than silently truncating history.

Run:

```
venv-unified/bin/python -m pytest -q tests/client/test_cord_invite_links.py tests/client/test_cord_invite_links_full_app.py
```

The runtime test independently derives the token HKDF key, verifies signatures and actual reader folds, checks encrypted storage, stale-device edits, unknown fields, acknowledgment failure, cancellation, account/control changes, original-link recovery, and concurrent minting. Real Chrome cases at 1280 and 390 pixels check label escaping, secret-free DOM, clickable copy controls, and no publication when opening the manager. Final refounding persistence/ACK recovery is covered by the CORD-06 suite; the creator test checks the handoff contract.
