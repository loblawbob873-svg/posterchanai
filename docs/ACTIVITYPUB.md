# The fediverse server (ActivityPub)

PosterChan can be a fediverse server: Mastodon, Pleroma, Misskey, GoToSocial and the rest can find,
follow, reply to and boost this node's users, and they can follow fediverse accounts back — with
**Nostr as the only store**. There is no posts table.

Turn it on in **Admin → Social → Fediverse server (ActivityPub)**. It is off by default, and every
ActivityPub address answers 404 until it is on.

## Who is on the fediverse

**Every local user, automatically.** Each name in this node's NIP-05 registry (Admin → Relay →
NIP-05 names) is `@name@<domain>`. Nobody signs up; a user's signing key is created the first time
another server asks for them. The only exclusion is an account blocked on the relay.

`<domain>` is the **Domain** setting, and blank means the NIP-05 domain — so `name@domain` on Nostr
and `@name@domain` on the fediverse are one identity. Changing the domain later breaks existing
follows (every follower knows the old address).

## Where things are kept

| What | Where |
|---|---|
| A post, like, boost or profile that arrives | a normal Nostr event, signed by the author's **puppet** key (the same deterministic key the Pleroma bridge gives that person), with a NIP-48 `proxy` tag back to the original, deduped in `FediBridgeDelivered` |
| A user's own posts going out | nothing is copied: the delivery loop reads them from the relay, and `/ap/objects/<event id>` serves them from the relay |
| Followers, follows, signing keys, the delivery position | operator-signed, NIP-44-encrypted kind-30078 documents on this node's relay (`pcai:ap:*`), one per item |

## How it fits the existing Nostr ↔ fediverse bridge

The two are one system, not two:

* **One identity.** A fediverse account gets the same puppet key from ActivityPub as from the
  Pleroma timeline mirror, and a note that arrives both ways is stored once (both check the note's
  canonical URI in `FediBridgeDelivered` first).
* **Nothing is sent twice.** A user whose Nostr activity already reaches the fediverse through their
  own linked Pleroma account (the write-back whitelist) is not also sent from `@name@<domain>`.
  Their account still exists and can be followed and replied to.
* **No loops.** The Pleroma mirror does not import posts by `@anyone@<our domain>` — they are our own
  users, already on the relay. A fediverse reply to one of them threads under their real post.
* **Write-back works on either kind of note.** Replying to, liking or boosting a note that came in
  over ActivityPub works through a linked Pleroma account exactly as for a mirrored one.

## What gets in

A post is stored only if a user here follows its author, or it mentions a user here, or it replies to
one of their posts — otherwise anybody could fill the relay by posting to its inbox. Followers-only
and direct posts are never stored: a Nostr kind-1 is public, so storing one would publish a private
post.

## Blocking instances

On the **relay**, not here: Admin → Relay → blocked bridges/relays. Every event this stores carries a
`proxy` tag naming its original instance, and the relay refuses blocked ones on ingest. The
ActivityPub side reads that same list so it never fetches from, or delivers to, a blocked instance.

## Following someone on the fediverse

Search for `@user@their.server` in the client. The node finds the account and opens its profile;
following it there is sent as an ActivityPub Follow, and their posts arrive from then on.

## What goes out

| Nostr | ActivityPub |
|---|---|
| a post (kind 1) | Create(Note) to followers — replies only when the parent is on the fediverse or is one of our users' posts (a reply deep in a Nostr-only thread would arrive with no context) |
| a comment (kind 1111) | the same, as a reply |
| a repost (6) / reaction (7) | Announce / Like of a fediverse note or one of our users' posts |
| a deletion (5) | Delete / Undo |
| a profile (0) | Update(Person) |
| a contact list (3) | Follow / Undo(Follow) for fediverse accounts added or removed |

## Web server

`/ap/*`, `/.well-known/webfinger`, `/.well-known/nodeinfo` and `/nodeinfo/*` must reach the app with
the `Host` header and path untouched and uncached — see `nginx/posterchanai.conf.example`. The paths
are under `/ap/` so they never collide with a `/inbox` 410 rule left behind by a retired Pleroma.

Code: `app/services/activitypub/` (the module docstrings are the detail), `app/routers/activitypub.py`.
Tests: `tests/test_activitypub.py`.
