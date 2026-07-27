# Commands & bot usage

The same command set is shared across every interface (web UI, Telegram, Pleroma,
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
| `ytdl <url>` | Download audio (MP3 default); `ytdl video <url>` for video (YouTube/X/Nitter — Nitter links resolve via X.com). Add `clip <start> <end>` and/or `compress`, e.g. `ytdl video <url> clip 0:10 0:30 compress` |
| `torrents <query>` / `nyaa <query>` | Torrent search / anime torrents |
| `dailynews <source>` / `news <source>` | Headlines (e.g. `news drudge`) |
| `4chan [g\|pol\|h]` | 4chan catalog browser |
| `geni <prompt>` | Generate an image |
| `musicgeni <style prompt> [\| lyrics]` | Generate a song (ACE-Step). Web UI + Telegram only. See [MUSIC.md](MUSIC.md) |
| `videogeni <prompt> [\| negative]` | Generate a short video (native diffusers Wan2.1). Web UI + Telegram only. Configure in Admin → Video |
| `screenshot <url>` | Full-page website screenshot (aliases `shot`, `ss`) |

## Productivity & system

| Command | What it does |
|---|---|
| `mail <to> [subject] <body>` | Send email |
| `remind <what> <when>` | Set a reminder in natural language — e.g. `remind open the oven in 10m`, `remind me next tuesday to call mom`. The LLM parses the time (exact relative phrases like `in 10s` parse directly); you're alerted in the web UI (full-screen pop-up) and on Telegram (if linked). Timezone is auto-detected from your browser (IANA, DST-aware) — no setup. |
| `reminders` | List your pending reminders, each with a clickable **Cancel** (web buttons / Telegram inline keyboard). Also `remind cancel <id>`. |
| `pin <query>` | Save (pin) a search you run often — e.g. `pin ai news`, `pin latest xrp news and price`. |
| `pins` | List your pinned searches, each with **Run** ▶ and **Delete** 🗑️ (web buttons / Telegram inline keyboard). Also `pin delete <id>`. Aliases: `savedsearches`, `saved`. |
| `translate <text> to <lang>` | Translate text |
| `files <query>` | Search your stored files |
| `logs` | System-health report (admin) |
| `node …` | Remote node management (admin/allowlisted): `node <name> <cmd>`, `node all <cmd>`, `node agent <name> <goal>`, `node list`, `node jobs`, `node log <id>`, `node kill <id>` |

## Media tools (attach a file)

| Command | What it does |
|---|---|
| `compress` | Shrink an attached image or video |
| `clip <start> <end>` | Trim an attached video, e.g. `clip 0:10 0:30` |
| `convert` | Images → PDF, or a PDF → images |
| `flashcards` | Turn an attached PDF, image, slide deck (PPTX) or Word doc (DOCX) into an interactive multiple-choice study quiz (aliases: `cards`, `study`, `quiz`) |

### Flashcards (study tool)

Attach study material and send `flashcards` to generate a multiple-choice quiz from it (the LLM
writes the questions, answer options and explanations; math problems show the answer **plus
worked steps**). Cards are ephemeral — they aren't saved.

- **Web UI:** animated cards — click an option for instant ✓/✗ + explanation, then Prev/Next/Shuffle.
  Equations render with KaTeX (self-hosted).
- **Telegram:** upload the file and tap **🎴 Flashcards** (or send the file captioned `flashcards`).
  You get an image card with answer buttons; tap an answer to reveal ✓/✗ + explanation, then
  ◀ Prev / Next ▶ / ↻ Restart, with a running score.
- **Inputs:** PDF, images (OCR), PPTX slides, DOCX/XLSX. Text PDFs/slides give the best results.
  Images rely on OCR, which is unreliable on dense screenshots. **On Telegram, send a screenshot as
  a *file/document*, not as a photo** — photos are compressed and OCR will read nothing.

## Image effects (attach an image)

**Stamps / overlays** (stay an image): `meme <text>`, `dildo`, `poo`, `cum`, `blood`,
`bullethole`, `fire`, `gay`, `blacked`, `kosher`, `barked`, `consider`, `chimp`, `clay`.

**Music / clip videos** (image → short MP4 with audio): `hava`, `indian`, `yakety`, `yamete`,
`curb`, `depressing`, `fahh`, `helpme`, `gong`, `fbi`, `redeem`, `gigity`, `beavis`, `smell`,
`hood`, `akbar`, `retard`, `whoabuddy`, `sopranos`, `cheers`, `munsters`, `happydays`,
`dontwanttowait`, `strangerthings`, `adamsfamily`, `xmen`, `futurama`, `charliesangles`,
`differentstroke`, `seinfeld`, `onepiece`, `overtaken`, `freebird`, `kanye`, `darkness`,
`bike`, `jobs`, `ree`, `liberal`, `moving`, `harlem`, `wasteland`, `mixalot`, `thug`,
`feltedtables`, `prayer`, `nakedman` (a fat cartoon man dances with a huge penis over the
image → 8s clip), `vibe` (a cute anime girl dances over the image → 8s clip), `rebecca`
(Rebecca dances with a thumbs up over the image → 8s clip), `makima` (Makima finger-guns the
image — muzzle flashes and gunshots, no music → 8s clip).

**Enhance** (generic — no gag, just make a post stand out): `glow` (on an attached image →
gentle breathing zoom + colour pop + a soft light sweeping across → short MP4), `glow <text>`
(no image → a glowing neon text-card post), `alive [subtle|normal|strong]` (3D parallax: the
photo gains real depth motion). Use these when you just want a post to pop without picking one
of the gag effects above. On Telegram, typing `glow <text>` (or tapping 🌟 Glow it after `post`)
offers to share the card to your connected platforms.

### Motion & colour modifiers

Append a modifier to **any** effect to animate or recolour its output. Syntax:
`<effect> [movement] [glow] [trippy] [meme <text>]` — e.g. `dildo zoom`, `whoabuddy pulse trippy`,
`fire shake glow meme TOP TEXT`.

There are two kinds, and the rule is simply **one movement + any of the looks**:

| Movement (pick ONE) | Effect |
|---|---|
| `zoom` | Slow Ken Burns zoom-out pan |
| `shake` | Strong camera shake |
| `medshake` | Gentler camera shake |
| `beginshake` | Shakes hard at the start, then settles |
| `pulse` | Rhythmic zoom in/out (bass-thump) |
| `alive` | 3D parallax — needs a STILL, so it's skipped on effects that output a video |

| Look (stack freely) | Effect |
|---|---|
| `glow` | Colour pop + a sweeping light over the real frames — e.g. `dildo zoom glow`, `alive glow` |
| `trippy` | Psychedelic hue-cycle over the real frames |

Each movement is a full re-render of every frame, so two of them would only fight over the same
frames — a second one is **refused with the reason** rather than silently dropped, which is what
older builds did (`curb zoom glow` quietly rendered as plain `curb glow`). The looks recolour the
frames a movement produced, so they layer on top of it and on each other in a fixed order
(movement → `glow` → `trippy`), meaning the same set of modifiers renders the same however you
type it.

`glow` and `alive` also work as standalone effects, and compose as modifiers: `alive glow` = 3D
parallax with the glow look layered on. The web client's Effects studio greys out any modifier the
picked effect can't take; Telegram offers the same set as its two-column motion menu (left =
movement, right = the same movement **+ trippy**); typed combos like `dildo zoom trippy` work in
the web UI, Telegram and the fedi bots alike.

**Multiple images → one movie:** attach several images to an audio/clip effect (whoabuddy, prayer,
sopranos, the TV themes, …) and they play as a **slideshow in upload order** over the one audio
track; `alive` plays each image's own 3D-parallax orbit in order. (Effects that transform each
image individually — `thug`, overlays — still use the first.) On Telegram, send the photos as one
**album**; a no-caption album shows the action menu, or add a caption like `whoabuddy`.

A trailing `meme <text>` burns an outlined caption on last; the caption text is never mistaken
for a modifier (only trailing modifier tokens are consumed), so `meme so trippy bro` keeps its
full caption.
