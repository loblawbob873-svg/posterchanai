# Why Nostr Users Should Run PosterChan AI's Built-in Nostr Relay

![PosterChan AI Nostr Relay](https://image.nostr.build/f10e630ccd7ea8247e4c9ac4c4326b7ae7376d28ae939b70a7c347fe4a17bde5.png)

Nostr is beautifully simple: you sign notes, relays pass them around, clients read
them. But the moment you actually *live* on Nostr, the cracks show. Your feed fills
with spam. Profiles won't load. Search barely works. Your follows post to relays you
don't connect to, so you miss half their notes. You end up juggling a dozen relays and
still feel like you're missing things.

PosterChan AI ships a **built-in Nostr relay** that fixes all of that — and it's
genuinely smart about it. It's not a dumb pipe. It runs inside your self-hosted
PosterChan AI instance, understands a **web of trust**, and quietly does the boring,
hard work that makes Nostr feel good. Optionally, it sits right next to PosterChan AI's
powerful AI bots and features — so the same box that filters your feed can also run
your AI assistant.

Here's what it does for you.

## 1. It's a bouncer

The relay only ever stores notes from people inside your **web of trust** — your seeds
and the accounts they follow. Random spammers, bots, and impersonators never make it
through the door. You set the guest list; the relay enforces it on every write *and*
every sync. Combined with per-account blocklists, it's a personal bouncer that keeps the
noise out so your timeline is just signal.

## 2. It's a search relay

Full-text **search** (NIP-50), backed by a real SQLite FTS5 index over note content.
Point your client's search at it and actually find things — fast. Most relays don't do
search at all; yours will.

## 3. It fetches profiles automatically

Ever seen a feed full of `npub1abc…` with no names and no avatars? The relay
**auto-downloads kind-0 profiles** for everyone in your web of trust, so names and
pictures are always there. No more faceless timelines.

## 4. Web of Trust

The heart of it. The relay builds your trust graph from a set of **seed npubs** plus the
people they follow (depth 1), or even **friends-of-friends** (depth 2, pruned so it
stays sane). That graph is what decides whose notes you keep — and it rebuilds itself
**daily**, with a one-click manual refresh whenever you want.

## 5. It fetches notes from the relays *you* choose

You tell it which upstream relays to pull from, and it keeps a **live connection** to all
of them — streaming in new notes from your web of trust the instant they're posted, in
real time. No more "my follow posted an hour ago and I'm just seeing it." It aggregates
the relays you care about into one clean stream.

## 6. A one-stop-shop relay

Timeline, **search**, profile lookup, **relay-list (outbox) lookup**, long-form
**articles**, **private DMs**, and your own outbox for posting — all from a single relay.
Point your client at it and a huge amount of Nostr Just Works, without stitching together
five specialized relays.

## 7. Auto-cleanup

It manages its own storage. Old notes get **automatically cleaned up** after a window you
choose — while your **profiles and contact lists are kept forever**. So your identity and
follow graph never disappear, but you never drown in years of stale notes either.

## 8. Word filtering

Beyond the web-of-trust bouncer, you can **filter by word or phrase** (and by
language/script). Don't want to see a certain term? Add it, and matching notes are
rejected on the way in — and existing ones purged. Your relay, your rules.

## 9. Download your Nostr data with one click

Want everything you've ever posted in one place? Hit **"Sync my posts to the relay"** and
it pulls your **entire history from your configured relays into your own relay** — one
click, your data, on your server. Your archive, finally yours.

---

## The point: your relay, on your terms

Every one of these runs **on your own box**, under your control — no third-party relay
operator deciding what you see or selling your data. And because it's part of PosterChan
AI, the same self-hosted stack can also give you AI chat, image/video/music generation,
and Telegram/Matrix/Nostr bots if you want them. The relay is great on its own and even
better as part of the whole.

It speaks the standards too — NIP-01, NIP-11, NIP-17 (DMs), NIP-23 (articles), NIP-50
(search), NIP-65 (lookup), and NIP-77 (negentropy sync) — so your favorite client just
works.

Spin it up, point your client at `wss://your-relay/`, and let it be the bouncer,
librarian, and archivist your Nostr experience always needed.

**Documentation:**
https://github.com/loblawbob873-svg/posterchanai/blob/main/docs/RELAY.md
