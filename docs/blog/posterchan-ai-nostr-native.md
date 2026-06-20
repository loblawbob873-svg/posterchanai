# Your AI, on Nostr: PosterChan AI is now Nostr-native to the core

Nostr nailed the hard part: **you own your identity and your data.** A keypair no platform can
revoke, content no server can quietly memory-hole. But look at the rest of your stack and the
sovereignty leaks out everywhere — your "assistant" runs on someone else's GPUs behind someone
else's API key, logging every prompt; your settings and history live in some app's database you'll
never see, on terms you didn't write.

**PosterChan AI** closes that gap. It's a self-hosted AI assistant + bot platform whose **datastore
is a Nostr relay**. Not "an app with a Nostr integration" — the relay *is* the database. Your
settings, your account, your API keys, your AI chat history: all **NIP-44-encrypted Nostr events**,
signed by your node, living in your own relay on your own box. You log in with your **Nostr key**,
and the whole UI is a full Nostr client. Same machine runs your models, your media, your relay, and
your bots. Your keys. Your hardware. Your rules.

---

## Your data is Nostr events — not rows in someone's DB

Under the hood PosterChan AI runs a built-in **web-of-trust relay on PostgreSQL**, and that relay is
the source of truth for the whole app. Every piece of app state is a **`kind-30078` (NIP-78)
parameterized-replaceable event**, NIP-44-encrypted and signed by the node's operator key:

- `pcai:setting:<key>` — one replaceable doc per admin setting (change one → it replaces just that one).
- `pcai:user:<npub>` — your account: identity, admin, feature caps.
- `pcai:usercfg:<npub>` — your personal settings (mail, social tokens, feeds…), encrypted.
- `pcai:apikey:<id>`, `pcai:bot:<id>`, `pcai:conv` / `pcai:msg` — API keys, bot config, AI chats.

The Postgres tables are just a **hydrated read-cache** — wipe them and they rebuild from the relay.
There's no SQLite, no proprietary schema, no app-specific lock-in. If it matters, it's an encrypted
event you hold. (Each node auto-mints its own operator key on first boot, so this works out of the
box.)

## Log in with your key. The UI *is* a Nostr client.

No email, no password, no "sign up." You authenticate with **NIP-07 (extension)** or **NIP-46
(remote signer like Amber)** — your secret key never touches the server. And the web app you land on
is a full, fast, cyberpunk **Nostr client**: home/global timelines, threads, profiles with NIP-05
verification, DMs (NIP-17), long-form articles (NIP-23), zaps, GIFs, full-text search, Blossom file
management — installable as a PWA. It talks only to your relay.

## A genuinely capable AI — in the client *and* in your replies

PosterChan AI is, first, an assistant — and it runs on **your** models (self-hosted Qwen-class LLMs +
SDXL image models on a single consumer GPU; an Intel Arc A770 and an RTX 3060 in production), or any
OpenAI-compatible cloud endpoint if you prefer. From the built-in **PosterChan AI** chat tab (and as
a Nostr bot you can tag in a note):

- **Chat** — a real, uncensored assistant you host yourself, with function-calling.
- **`geni`** — text-to-image, uploaded to your own Blossom server and embedded inline.
- **`musicgeni`** / **`videogeni`** — text-to-song and text-to-video (web/Telegram).
- **`ytdl mp3` / `ytdl video`**, **`screenshot`**, **`translate`**, **`search`** (self-hosted SearXNG),
  **`images`**, **`news`**, media tools (`compress`/`clip`/`convert`/`meme`/`glow`) — all behind one
  chat box. Type `help` for the full list.

Tag the bot in a note and it answers in-thread like a good citizen: it p-tags who it replies to,
threads correctly, and posts media as proper attachments. (Heavier generators stay on the web UI /
Telegram, not the open Nostr listener — no mass-abuse surface.)

## The relay does the boring, hard work

It's a *smart* relay, not a dumb pipe:

- **Web-of-Trust bouncer** — only stores notes from your trust graph (seeds + their follows), so
  spam and impersonators never land. Rebuilt daily.
- **Real search (NIP-50)** over a Postgres `tsvector` index — most relays don't search at all.
- **No faceless feeds** — auto-fetches kind-0 profiles so names and avatars always render.
- **Never miss a note** — a live firehose from the upstream relays *you* choose, plus windowed sync
  to backfill gaps, plus automatic **thread completion** (walks up missing parents).
- **Your outbox + DM inbox** — re-broadcasts what you publish; accepts gift-wrapped DMs (NIP-17/44/59).
- **Bounded + self-maintaining** — auto-prunes old notes (default 30 days) but keeps profiles and
  follow lists forever.
- **Resilient federation** — upstream connections try Tor first and fall back to a direct connection
  if Tor is flaky.

Standards-complete: NIP-01/02/09/11/17/22/23/40/44/45/50/59/65/77. Pure Python, built into the app —
no strfry/khatru, no extra daemon.

## Scale it across machines — without duplicating work

Run a **primary node** (full relay: WoT + NIP-05 + the social/notification schedulers) and any number
of **processing nodes** (GPU compute + load balancing) with two switches in Admin → Relay:

- **Web of Trust off** turns a node into a pure local store — no trust-graph, firehose, sync, or
  NIP-05 work, so it never duplicates the primary.
- **Send-only** keeps a node broadcasting its own events upstream while never mirroring upstream back,
  so its local DB stays lean.

Add a node's **operator npub** to another node's WoT seeds (one click — *Copy npub* on the Relay tab)
and they federate cleanly.

---

## The whole point: sovereignty without the duct tape

Self-hosting usually means stitching five projects together and praying. PosterChan AI is **one
stack**: the relay that filters your feed and stores your data also serves your AI chat, image
generation, media tools, and your Telegram / Matrix / Nostr bots. No API keys to megacorps. No
prompts logged by a third party. No relay operator deciding what you see or monetizing your graph.

If Nostr is about owning your identity, PosterChan AI is about owning the **tools** around it — and
storing them the same way Nostr stores everything else: as events **you** sign and **you** keep.

**Get started:** install PosterChan AI, point your reverse proxy at the relay, open the web client,
and log in with your Nostr key.

- Relay docs: [docs/RELAY.md](RELAY.md) · Datastore design: [docs/NOSTR_DATASTORE.md](../NOSTR_DATASTORE.md)
- Project: https://github.com/loblawbob873-svg/posterchanai

Your keys. Your models. Your relay. Your events. Your box.
