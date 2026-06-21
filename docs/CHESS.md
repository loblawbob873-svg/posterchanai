# ♟️ #chesstr — playing chess over Nostr

`#chesstr` lets two Nostr users play a full game of chess where a **bot is the board + referee**.
Every game is **public** (kind-1 posts + replies), tagged **#chesstr**, so anyone can spectate and
the games gather interest. Games **never expire** — a single game can span days, just waiting for
the next reply.

This doc is the **how-to-test** guide. (Internals are in `botframework/chessListener.py` +
`botframework/chess_render.py`; admin setup is in `docs/BOTS.md`.)

## 1. One-time setup (admin)

1. **Admin → Bots → Add bot.**
2. Set **Platform = Nostr**, give it a name (e.g. `ChessBot`).
3. Click **✨ Generate identity**. This automatically:
   - mints the bot's `nsec`,
   - grants it **Blossom** upload access (so it can post board images on the built-in server),
   - publishes its **profile** (name, avatar, optional NIP-05),
   - makes the **operator follow it** and refreshes the relay **Web of Trust** — so its posts are
     stored & served immediately (no waiting for the daily WoT rebuild).
4. Tick the **Chess referee (#chesstr)** feature, then **Save bot** and toggle it **On**.

Media uploads always go to **this server's built-in Blossom** — there's no media host to configure.

## 2. Starting a game

Two ways:

- **From the app:** open **Games → Chess** in the web client. Search a player by **npub** or
  **name@domain (NIP-05)**, then click **Challenge**. The invited player is **White** and "accepts"
  by making the first move.
- **By hand on Nostr:** post a note that mentions **the bot and your opponent** and contains the
  word *chess*, e.g. `chess nostr:npub1opponent…` (also tag the bot). The poster is White.

The bot replies in-thread with the **cyberpunk board** and an invitation message. **Your own pieces
are numbered** on the board image.

## 3. Making moves

Reply to the bot's latest board post with any of:

| Form | Example | Meaning |
|------|---------|---------|
| **number + square** | `1 d4` | move *your* piece labelled **1** to **d4** |
| SAN | `Nf3`, `exd5`, `e8=Q` | standard algebraic |
| UCI | `g1f3` | from-square + to-square |
| castling | `O-O`, `O-O-O` | king/queen-side castle |
| resign | `resign` | concede the game |

Or, in the web client's **Games → Chess** tab, just **tap your piece then its destination** — the
move is sent for you (as UCI). The board there shows your live games and whose turn it is.

The bot **validates every move** — illegal moves are rejected with a hint listing where that piece
can actually go. It only accepts moves from the player **whose turn it is**.

## 4. End of game

The bot detects **checkmate**, **stalemate**, and **draws** (insufficient material, repetition,
50/75-move) and posts a final **GAME OVER** board with the result. `resign` ends it immediately.

## 5. Edge cases / behaviour

- **Numbering** is recomputed every turn for the side to move (stable a1→h8 order) and drawn on the
  board, so "piece 1" always refers to what you see.
- **One active game per player:** if you start a **new** game while you already have an unfinished
  one, the older game is **abandoned** (it posts an "abandoned" notice and stops accepting moves).
- **Never expires:** the game state is a replaceable **kind-30078** app-data event keyed by the
  game's root note id, so it survives restarts and waits indefinitely for the next move.
- **Public by design:** the board posts and move replies are normal public notes tagged `#chesstr`.

## 6. Quick local test

1. Create + enable a chess bot (step 1) on this node.
2. In the web client, **Games → Chess**, challenge a second account you control (or post the
   `chess …` note by hand from another client).
3. As the challenged (White) account, reply `1 d4` (or tap the board) — confirm the bot posts an
   updated board tagging the other player.
4. Play a quick **fool's mate** to see the GAME OVER card: White `f3`, Black `e5`, White `g4`,
   Black `Qh4#`.
