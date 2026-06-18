# PosterChan AI: Your Own AI Bots *and* Your Own Relay — All on Nostr

![PosterChan AI on Nostr](https://image.nostr.build/f10e630ccd7ea8247e4c9ac4c4326b7ae7376d28ae939b70a7c347fe4a17bde5.png)

Nostr got one thing exactly right: **you own your identity and your data.** No platform
can deplatform a keypair. So why is the rest of your stack still rented? Your AI lives on
someone else's servers, behind someone else's API key, logging every prompt. Your "smart"
tools phone home.

**PosterChan AI** is the other half of the sovereign internet. It's a self-hosted stack
that runs **AI bots that live on Nostr** *and* a **smart, web-of-trust Nostr relay** — on
your own box, on consumer hardware, with no third-party AI API and no data leaving your
machine. Tag the bot in a note and it answers. Point your client at the relay and your
feed gets quiet, fast, and complete. Same box. Your keys. Your rules.

Here's what you actually get.

---

## Part 1 — An AI bot that lives in your replies

Mention the bot in a Nostr note and it replies in-thread, like any other account — except
it's **your** account, running **your** models, on **your** server. No OpenAI, no
Anthropic, no API bill, no telemetry. Everything below runs on self-hosted models (a
Qwen-class LLM, Stable-Diffusion-XL image models) on a single consumer GPU — it's been
deployed on an Intel Arc A770 and an RTX 3060.

What you can ask it to do, right from a note:

- **Just talk.** Reply to it or tag it and it answers conversationally — a genuinely
  capable uncensored assistant that you host yourself.
- **`geni <prompt>` — generate images.** Describe a picture and it posts one back,
  uploaded to your own [Blossom](https://github.com/hzrd149/blossom-server) media server
  and embedded in the reply. Your prompts and your images never touch a cloud service.
- **`search <query>` — real web search.** Backed by a self-hosted
  [SearXNG](https://github.com/searxng/searxng), it returns an AI-written summary plus
  sources. Ask the bot a question about the live web and get an answer, in-thread.
- **`images <query>`** — pulls and posts real image results.
- **`news <source>`** — fetches current headlines.
- **`/narrate <text>` — text-to-speech narration videos**, posted as media.
- **`screenshot <url>`** — full-page screenshot of any site.
- **`ytdl <url>`** — grab audio (or video) from a link.
- **Media tools on attachments** — `compress`, `clip`, `convert`, `meme <text>`,
  `glow <text>`: attach or link a file and the bot processes it and posts the result.

It's the same brain behind PosterChan AI's Telegram and Matrix bots, so you get one
self-hosted assistant reachable from wherever you already are — and on Nostr it behaves
like a good citizen: it **p-tags** the people it replies to, threads correctly, and
posts media as proper attachments.

> A note on abuse surface: heavier generators (music, video) are intentionally kept to the
> web UI and Telegram, not the open Nostr listener. The Nostr bot does the things that make
> sense in public replies — chat, images, search, narration, media — and nothing that
> invites mass abuse.

---

## Part 2 — A relay that does the boring, hard work for you

Running on Nostr long enough teaches you the pain: spam in the feed, faceless
`npub1abc…` timelines, broken search, and follows posting to relays you don't connect to
so you miss half their notes. PosterChan AI ships a **built-in relay** that fixes those —
and it's smart, not a dumb pipe.

- **It's a bouncer (Web of Trust).** It only ever stores notes from inside *your* trust
  graph — your seed npubs and the people they follow (depth 1), optionally
  friends-of-friends (depth 2, pruned to stay sane). Spammers and impersonators never get
  through, on every write *and* every upstream sync. The graph rebuilds daily.
- **It's a search relay (NIP-50).** A real SQLite FTS5 index over note content. Point
  your client's search at it and actually find things — most relays don't do search at all.
- **It fixes faceless feeds.** It auto-downloads kind-0 profiles for everyone in your
  trust graph, so names and avatars are always there.
- **It never misses a note.** It keeps live connections to the upstream relays *you*
  choose and streams in your follows' notes in real time, into one clean feed — and
  windowed sync back-fills any gap if it was offline.
- **It completes broken threads.** When a synced reply is missing its parent, the relay
  walks up and back-fills the ancestors so conversations actually make sense.
- **It's your outbox.** Notes you publish through it get re-broadcast to the wider
  network; notes it pulled in are never re-broadcast (no loops). Point all your clients at
  it and it becomes your single outbox.
- **It's a DM inbox (NIP-17/44/59).** Gift-wrapped DMs addressed to you are accepted and
  served back, stored privately, never re-broadcast.
- **It archives your history.** One click — "Sync my posts to the relay" — pulls your
  entire post history from your configured relays into your own relay. Your data, your box.
- **It manages itself.** Old feed notes auto-clean after a window you set, while
  **profiles and contact lists are kept forever** — your identity and follow graph never
  vanish, but you never drown in stale notes.

### Standards-complete

The relay speaks a broad, current slice of the protocol so your favorite client just
works: **NIP-01** (core), **NIP-02** (contacts), **NIP-09** (deletions), **NIP-11**
(relay info), **NIP-17/44/59** (private DMs), **NIP-22/23** (comments + long-form),
**NIP-40** (event expiration — honoured end-to-end: expired events are rejected, hidden
from every read, and swept from disk), **NIP-45** (COUNT), **NIP-50** (search),
**NIP-65** (relay lists / outbox lookup), and **NIP-77** (negentropy set reconciliation
for efficient sync). Ephemeral events (kind 20000–29999) are delivered live but never
persisted, exactly per spec.

It's also honest infrastructure: pure Python built into the app, no external relay binary
(no strfry/khatru), no extra daemon, on-disk WAL storage that scales to many GB, and a
libsecp256k1 fast-verify path so ingest stays cheap.

---

## The whole point: sovereignty without the duct tape

Self-hosting usually means stitching together five projects and praying. PosterChan AI is
**one stack**: the same self-hosted box that filters your feed and serves your relay also
runs your AI chat, image generation, and your Telegram/Matrix/Nostr bots. No API keys to
megacorps. No prompts logged by a third party. No relay operator deciding what you see or
monetizing your social graph.

If Nostr is about owning your identity, PosterChan AI is about owning the *tools* around
it — the assistant, the search, the media, and the relay that ties your feed together.

**Get started:** spin up PosterChan AI, enable the relay in **Admin → Relay** with your
seed npubs, point your client at `wss://your-relay/`, connect a Nostr key to the bot, and
tag it in a note.

- Relay docs: https://github.com/loblawbob873-svg/posterchanai/blob/main/docs/RELAY.md
- Project: https://github.com/loblawbob873-svg/posterchanai

Your keys. Your models. Your relay. Your box.
