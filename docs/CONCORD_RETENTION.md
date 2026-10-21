# Concord — Keep history

A **per-community** setting (community settings → *Keep history*): how long PosterChan keeps a
community's messages on a member's device. **Forever** is the default and never clears anything;
1 year / 90 days / 30 days age out older messages on each device. Set by whoever may edit the
community's settings (`MANAGE_METADATA`), next to *Disappearing messages*.

## Where it is stored — and why there

Checked against the CORD specification
([concord-protocol/concord @ b84554e](https://github.com/concord-protocol/concord/tree/b84554ea5dd47510057a580fa2f8587b4399ad17),
identical to `main` on 2026-10-20):

* **CORD-02 §6**: top-level fields of the community-metadata entity are *reserved for the protocol*;
  a client's own settings go in the optional `custom` object, under a prefixed key. So the value is
  `custom["posterchan/history_retention"]` — a whole number of days, absent = Forever. A top-level
  `history_retention` would have been a protocol field we had no right to invent.
* **CORD-02 §6 round-tripping**: an editor MUST carry through fields it does not understand. The
  writer (`cord-reader.js createMetadataWrap`, `customPatch`) MERGES our key into the prior `custom`
  object (null removes only our key); another client's `rules` or `vector/…` keys survive every save,
  and a save that does not touch `custom` (a rename) carries it unchanged.
* **Malformed is Forever**: zero, negative, fractional, a string or anything out of range reads as
  Forever — the direction that keeps messages.

## What it is not

* **Not expiry.** Disappearing messages (**CORD-08**) is the protocol's way to delete messages:
  signed `expiration` tags, relays purge, every client hides. Keep history never adds an `expiration`
  tag and never sends a deletion — other clients would act on those. It is a policy for PosterChan's
  own device cache only. CORD-08 still wins: an expired message is refused, hidden and purged under
  Forever too.
* **Not applied to the control plane.** Control and guestbook streams are never trimmed whatever the
  setting (CORD-08 §1: those planes never expire, and the control plane must stay complete).

## The device cache (`concord-cache.js`)

The policy is recorded per stream (`pc.concord.keep.v1` in localStorage) by every cache write, because
rows already held are never rewritten and a per-row flag would miss them.

| Stream policy | 5,000-per-channel cap | 32 MB oldest-first sweep | Ages out |
|---|---|---|---|
| none (a community not read yet) | yes | yes | — |
| Forever | no | no | never |
| N days | no | no | rows older than N days |
| control / guestbook | no | no | never |

The 32 MB sweep used to evict oldest-first across every stream, i.e. the control plane first. It
reads the whole store, so it now runs at most once a minute. Kept history is never evicted to make
room: past 512 MB the app says so (`pc-concord-storage`, one notice per session) and the setting is
the way to shorten it.

Tests: `tests/client/test_cord_custom_retention.py` (spec rules, against the shipped reader),
`tests/client/test_concord_keep_history_cache.py` (the cache, in a real browser).
