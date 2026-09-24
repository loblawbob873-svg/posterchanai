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

**Every Nostr user, too** (the second switch): anybody this relay holds a profile for is on the
fediverse under a **readable handle** — their profile name plus four characters of their key
(`@dana_4b56@<domain>`), claimed once and kept (`pcai:ap:nick:<pubkey>` / `pcai:ap:nickof:<nick>`),
so it does not change when they rename. WebFinger answers with that handle (Mastodon requires the
subject to equal `preferredUsername@domain`); the actor id stays `/ap/users/<npub>`, and
`@npub1…@<domain>` still resolves to the same account. Fediverse people can follow, mention and reply to them — and because they do not
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
| A post, like, boost or profile that arrives | a normal Nostr event, signed by the author's **puppet** key (derived deterministically, so a person keeps one key), with a NIP-48 `proxy` tag back to the original, deduped in `FediBridgeDelivered` |
| A user's own posts going out | nothing is copied: the delivery loop reads them from the relay, and `/ap/objects/<event id>` serves them from the relay |
| Followers, follows, signing keys, the delivery position, blocks, handles | operator-signed, NIP-44-encrypted kind-30078 documents on this node's relay (`pcai:ap:*`), one per item |

## The Pleroma bridge is gone

This server replaced the old timeline mirror, write-back and personal-notification bridge, and
their code was removed. What they left behind that still matters: the puppet keys (a fediverse
person who was mirrored keeps the key they had), the `FediBridgeDelivered` dedup ledger (now
pruned by ActivityPub on the relay's retention window — `ledger.py`), and two settings the server
reads — **blocked instances** (`fedi_bridge_blocked_domains`) and whether fediverse posts are
shared with other Nostr relays (`fedi_bridge_broadcast`), both under Admin → Social → Fediverse server. Pleroma is still a bot
platform (Admin → Bots) for a node that runs a bot on a Pleroma account.

## What gets in

A post is stored only if a user here follows its author, or it mentions a user here, or it replies to
one of their posts — otherwise anybody could fill the relay by posting to its inbox. Followers-only
and direct posts are never stored: a Nostr kind-1 is public, so storing one would publish a private
post.

## Direct messages

A fediverse direct message to one of these accounts arrives as an encrypted NIP-17 DM from the
sender's puppet; a Nostr DM to a fediverse account's puppet is delivered to them as a direct
message (named by handle, `@user@server`). **This server reads the messages it carries** — that is what a bridge between two
encryption systems is, and the setting says so. **Anyone not blocked can write**: a message is
refused only when the sender's instance or account is blocked, or when the recipient's public mute
list (kind 10000) names the sender's puppet. Puppets publish a DM-relay list (kind 10050) naming
this relay, so any Nostr client knows where to send them.

## Emoji and reactions

Custom emoji travel both ways: a Nostr post's NIP-30 `emoji` tags become ActivityPub `Emoji` tags
(so `:blobcat:` renders on Mastodon/Pleroma), and an incoming post's `Emoji` tags become NIP-30
tags. A reaction goes out as a `Like` carrying the emoji in `content` and `_misskey_reaction` (what
Pleroma, Akkoma and Misskey read), with an `Emoji` tag for a custom one; an incoming emoji reaction
becomes a kind-7 with that emoji (and its NIP-30 tag). A NIP-25 downvote (`-`) is not sent — the
fediverse has no dislike, and a Like would say the opposite.

## A profile's posts (the outbox)

`/ap/users/<name>/outbox` is a real paged collection of the account's recent public posts, read
from the relay (`?page=true&max_id=…`), so a profile opened on another server is not empty before
anybody there has followed it.

## Blocking

**Blocked instances** (Admin → Social → Fediverse server) and the relay's **blocked
bridges/relays**, through one parser (`app/services/fedi_blocklist.py`): an instance line blocks it and its subdomains in both spellings of an
internationalised name (`嘟文.com` and `xn--j5r817a.com`), and a `user@host` line blocks that one
account. There is no third list.

**Blocking one fediverse person** is the relay's own pubkey blocklist: blocking their puppet npub
(Admin → Relay) refuses their activities at the inbox and stops delivery to them — the key is
matched against the puppet table AND re-derived from the actor, so it holds for a person the table
has not seen yet.

**A fediverse user blocking one of ours** (`Block`) is recorded (`pcai:ap:blocked:*`), drops their
follow of that member and the member's follow of them, and is undone by `Undo(Block)`. Those
records, together with public Nostr mute lists, are what the block bot reads
(`/api/community/blocks`, see docs/BOTS.md).

## Bringing your follows across

Settings → Fediverse → **Follow everyone you follow there** adds the accounts an old fediverse
account follows to your Nostr follows: type it as `name@server` — any account whose follow list is
public (Pleroma, Akkoma, Mastodon, GoToSocial), with no login. A server on your own network (whose
name resolves to a private address here) must be listed in Admin → Social → **Fediverse servers on
this network** (`activitypub_lan_hosts`), or the address guard refuses it. If the
relay does not take the new follow list, the button says so; it never reports a failure as "already
followed".

Whatever fediverse accounts are ALREADY in your follow list are followed from `@you@<domain>`
automatically (the worker's follow catch-up, one pass per member): a list written before this server
was switched on is never a "new" event for the delivery loop, so without it a member who had followed
hundreds of accounts before the server existed had a fediverse account that followed nobody.

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

## Security model (reviewed 2026-09-24)

Every rule below is a test in `tests/test_activitypub.py`, and each test was checked to FAIL with
its rule removed.

* **Only a server speaks for its own accounts.** An inbox request must be signed over
  `(request-target)`, `host`, `date` and `digest`; the key's owner must be on the keyId's host and
  publish exactly that key; the signer must be the activity's actor, and a Create's `attributedTo`.
  A Create signed by somebody ELSE (Mastodon forwarding a reply) is only a pointer: the post is
  fetched from its own server and handled as if its author had sent it.
* **An identity comes from the account's own actor document, never from what somebody says about
  it.** A Mention tag, an imported follow list and a boost all carry third-party claims (`href`,
  `acct`, name, avatar). Believed, one note could register `victim@mastodon.social` against an
  address the sender controls, and every later sighting of the real person reused it -- their key,
  their DMs, the right to delete their posts. Mentions and imports now resolve each account from
  its own server; `ensure_puppet` reuses a record by handle only for one person's two addresses on
  ONE server (`/@alice` beside `/users/alice`).
* **A readable handle already given out beats a registry name spelled the same**, and signup will
  not register a name that is somebody's fediverse address.
* **Budgets are charged to who signed, after verification**; unverified traffic is charged to the
  connection. Charged to the claimed host, anybody could make a real server's deliveries 429.
  Pending inbox work is capped per sender (one server cannot hold every slot), every outbound fetch
  has a total time limit, and new followers are capped per server per hour.
* **Outbound fetches connect to the address that was checked** (DNS rebinding), on port 443 only
  (a neighbour listed under "Fediverse servers on this network" excepted), and a refused signature
  never says why -- that error text would make the inbox a port scanner.
* **Remote images are https only** (emoji, avatars): anything else would let a remote server track
  readers or reach into their network.
* **Who blocked whom is private.** `/api/community/*` answers the bots' configured key, an admin's
  key or a peer node -- not any member's API key -- and one server can add at most three blockers
  to a member's leaderboard count.

## Compatibility

| Server does | This server |
|---|---|
| GoToSocial's fragment-less keyId (answers with an actor stub) | accepted; the owner is fetched and must publish the key |
| a Lemmy community / Guppe group relaying a post (Announce of a Create) | the post is fetched from its own server and stored, the group's boost with it |
| an edit (Update of a Note; also poll counts) | the stored copy is replaced; only by the author |
| an account deleting itself | confirmed with its own server (410/404), then nothing more is delivered to it |
| Article / Page / Question | title kept, poll options listed, an Article's summary is not a CW |
| alt text, blurhash, dimensions | both ways (NIP-92 `imeta` ⇄ attachment fields); untyped media stays a link |
| quote posts | both ways: `quoteUrl` + `_misskey_quote` + `quoteUri` out; a link and a `q` tag in |
| a reply whose parent is not here | the parent is fetched (one level); else an `r` tag says which thread |
| deleting a reply, reaction or boost | reaches every server it went to, as the right verb, even without a `k` tag |
| re-following after an unfollow | a new Follow id each time (Pleroma drops a repeated id) |
| host-meta, NodeInfo 2.0 and 2.1, `featured`, profile fields | served |
| the retired Pleroma's actor address `/users/<name>` | an ActivityPub request is redirected to `/ap/users/<name>` |
| a deleted post | 410 with a Tombstone |
| a server that keeps failing | rests (15 min, doubling to 4 h) with its deliveries queued |

Not supported yet: incoming Move (account migration), Flag (reports), outgoing polls, pinned posts
(`featured` is served empty), Undo of a Follow given only by id, and a delivery queue that survives
a worker restart.

## Web server

`/ap/*`, `/.well-known/webfinger`, `/.well-known/nodeinfo`, `/.well-known/host-meta` and `/nodeinfo/*` must reach the app with
the `Host` header and path untouched and uncached — see `nginx/posterchanai.conf.example`. The paths
are under `/ap/` so they never collide with a `/inbox` 410 rule left behind by a retired Pleroma.

Code: `app/services/activitypub/` (the module docstrings are the detail), `app/routers/activitypub.py`.
Tests: `tests/test_activitypub.py`, `tests/test_community_bots.py`.
