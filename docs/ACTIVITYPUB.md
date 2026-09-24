# The fediverse server (ActivityPub)

PosterChan can be a fediverse server: Mastodon, Pleroma, Misskey, GoToSocial and the rest can find,
follow, reply to and boost this node's users, and they can follow fediverse accounts back — with
**Nostr as the only store**. There is no posts table.

It is **on out of the box** (Admin → Social → Fediverse server (ActivityPub)): three switches —
talk to the fediverse, every Nostr user can talk to the fediverse, direct messages — all on unless
an admin turns one off. A node with no public domain still answers nothing, because there is no
address to be reachable at.

## Who is on the fediverse

**Every local user, automatically.** Each name in this node's NIP-05 registry (Admin → Relay →
NIP-05 names) is `@name@<domain>`. Nobody signs up; a user's signing key is created the first time
another server asks for them. The only exclusion is an account blocked on the relay.

**Every Nostr user, too** (the second switch): anybody this relay holds a profile for is
`@npub1…@<domain>`. Fediverse people can follow, mention and reply to them — and because they do not
read this relay, those replies and mentions are ALSO published to their own relays (NIP-65 read
relays), with the author's profile. What they write back to the fediverse (a reply to, mention,
like or boost of a fediverse post) goes out from their npub account. Keys for such accounts are made
on first use, at most 30 a minute.

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

## Direct messages

A fediverse direct message to one of these accounts arrives as an encrypted NIP-17 DM from the
sender's puppet; a Nostr DM to a fediverse account's puppet is delivered to them as a direct
message. **This server reads the messages it carries** — that is what a bridge between two
encryption systems is, and the setting says so. A fediverse account can message someone only if
that person follows it or wrote to it first. Puppets publish a DM-relay list (kind 10050) naming
this relay, so any Nostr client knows where to send them.

## Blocking instances

The lists that already exist — the fediverse bridge's **blocked domains** (Admin → Social) and the
relay's **blocked bridges/relays** — through one parser (`app/services/fedi_blocklist.py`) shared by
the bridge and ActivityPub: an instance line blocks it and its subdomains in both spellings of an
internationalised name (`嘟文.com` and `xn--j5r817a.com`), and a `user@host` line blocks that one
account. There is no third list.

## Going live on a node that ran the Pleroma bridge

Switch the bridge off (`fedi_bridge_enabled`). While it runs, a user on a linked Pleroma account
posts through it and ActivityPub sends only their follows; once it is off, ActivityPub carries
everything for them.

## Bringing your follows across

Settings → Fediverse → **Follow everyone you follow there** adds the accounts an old fediverse
account follows to your Nostr follows: your linked account, or — typed as `name@server` — any
account whose follow list is public (Pleroma, Akkoma, Mastodon, GoToSocial), with no login. If the
relay does not take the new follow list, the button says so; it never reports a failure as "already
followed".

Whatever fediverse accounts are ALREADY in your follow list are followed from `@you@<domain>`
automatically (the worker's follow catch-up, one pass per member): a list written before this server
was switched on is never a "new" event for the delivery loop, so without it a member who had followed
hundreds of accounts through the Pleroma bridge had a fediverse account that followed nobody.

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
