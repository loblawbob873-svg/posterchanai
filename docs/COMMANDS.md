# Commands & bot usage

The same command set is shared across every interface (web UI, Telegram, Matrix, Pleroma,
Misskey) via `app/services/command_service.py`. This page is the user-facing reference: **how
to drive each bot**, then **every command** grouped by what it does.

> Canonical source: `CommandService.COMMANDS` + `MOTION_EFFECTS`/`MOTION_ARGS`. If you add a
> command, update it there (and the per-interface wiring — see `CLAUDE.md`), then refresh this
> page.

## How to use each bot

| Interface | How you talk to it | Attaching media |
|---|---|---|
| **Web UI** (chat) | Type the command in the message box, e.g. `search cats`. Plain talk (no command) goes to the LLM. | Upload a file with the message; media commands (`compress`/`clip`/`convert`/effects) act on it. |
| **Telegram** | Type a command, **or** use the inline keyboards. Upload an image and tap **Effects** → an effect → the motion menu. Some flows (`clip`, captions) use reply prompts. | Send the photo/file in the same message; the bot caches it for the buttons. |
| **Matrix bot** | DM or `@mention`; commands run through `/command`. | Reply to (or send) a file with the command; it forwards the upload for media commands. |
| **Pleroma / Mastodon** | `@mention` the bot with a command, e.g. `@bot dildo zoom trippy`. `help` lists everything. | Attach the image/file to the post. |
| **Misskey** | Same as Pleroma — `@mention` + command, attach media. | Attach to the note. |

Plain conversation works everywhere — anything that isn't a recognised command is answered by
the LLM. The bots reply with TTS via `/narrate <message>`.

## Info & search

| Command | What it does |
|---|---|
| `search <query>` | Web search |
| `images <query>` | Image search |
| `yt <query>` | YouTube search |
| `ytdl <url>` | Download audio (MP3 default); `ytdl video <url>` for video (YouTube/X). Add `clip <start> <end>` and/or `compress`, e.g. `ytdl video <url> clip 0:10 0:30 compress` |
| `torrents <query>` / `nyaa <query>` | Torrent search / anime torrents |
| `dailynews <source>` / `news <source>` | Headlines (e.g. `news drudge`) |
| `4chan [g\|pol\|h]` | 4chan catalog browser |
| `geni <prompt>` | Generate an image |
| `screenshot <url>` | Full-page website screenshot (aliases `shot`, `ss`) |

## Productivity & system

| Command | What it does |
|---|---|
| `mail <to> [subject] <body>` | Send email |
| `translate <text> to <lang>` | Translate text |
| `files <query>` | Search your stored files |
| `budget` / `bills` / `pay <bill>` / `addbill` | Budget Manager (per-user `finance_api_key`) |
| `logs` | System-health report (admin) |
| `node …` | Remote node management (admin/allowlisted): `node <name> <cmd>`, `node all <cmd>`, `node agent <name> <goal>`, `node list`, `node jobs`, `node log <id>`, `node kill <id>` |

## Media tools (attach a file)

| Command | What it does |
|---|---|
| `compress` | Shrink an attached image or video |
| `clip <start> <end>` | Trim an attached video, e.g. `clip 0:10 0:30` |
| `convert` | Images → PDF, or a PDF → images |

## Image effects (attach an image)

**Stamps / overlays** (stay an image): `meme <text>`, `dildo`, `poo`, `cum`, `blood`,
`bullethole`, `fire`, `gay`, `blacked`, `kosher`, `barked`, `consider`, `chimp`, `clay`.

**Music / clip videos** (image → short MP4 with audio): `hava`, `indian`, `yakety`, `yamete`,
`curb`, `depressing`, `fahh`, `helpme`, `gong`, `fbi`, `redeem`, `gigity`, `beavis`, `smell`,
`hood`, `akbar`, `retard`, `whoabuddy`, `sopranos`, `cheers`, `munsters`, `happydays`,
`dontwanttowait`, `strangerthings`, `adamsfamily`, `xmen`, `futurama`, `charliesangles`,
`differentstroke`, `seinfeld`, `onepiece`, `overtaken`, `freebird`, `kanye`, `darkness`,
`bike`, `jobs`, `ree`, `liberal`, `moving`, `harlem`, `wasteland`, `mixalot`, `thug`,
`feltedtables`.

### Motion & colour modifiers

Append a modifier to **any** effect to animate or recolour its output. Syntax:
`<effect> [motion] [trippy] [meme <text>]` — e.g. `dildo zoom`, `whoabuddy pulse trippy`,
`fire shake meme TOP TEXT`.

| Modifier | Effect |
|---|---|
| `zoom` | Slow Ken Burns zoom-out pan |
| `shake` | Strong camera shake |
| `medshake` | Gentler camera shake |
| `beginshake` | Shakes hard at the start, then settles |
| `pulse` | Rhythmic zoom in/out (bass-thump) |
| `trippy` | Psychedelic hue-cycle — **composes on top** of any one motion above (the only stackable modifier; geometry motions don't stack with each other) |

In Telegram these are the two-column motion menu (left = motion, right = the same motion **+
trippy**); typed combos like `dildo zoom trippy` work in the web UI, Matrix, and the fedi bots.

A trailing `meme <text>` burns an outlined caption on last; the caption text is never mistaken
for a modifier (only trailing modifier tokens are consumed), so `meme so trippy bro` keeps its
full caption.
