"""#connect4 — Connect Four over Nostr, refereed by the bot. Same pattern as chess/ttt/hangman:
START with "connect4 @opponent" (or "c4 @opponent"); with no opponent you play the bot. MOVE by
replying with a column number 1-7 (the disc drops to the lowest empty slot). The bot validates,
renders a cyberpunk board, posts it tagging the other player, and calls 4-in-a-row / draw. State is
a replaceable kind-30078 doc keyed by the game root id (never expires). Every post carries
#connect4 #nostr #gamestr; only the opening + final posts federate (mid-game is local-only)."""
import os
import re
import sys
import json
import time
import fcntl
import hashlib
import tempfile

script_dir = os.path.dirname(os.path.abspath(__file__))
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)

import connect4_render
import nostr as _nk
from config import NOSTR_NSEC
from app.services.nostr import event as _ev

_KIND_APP = 30078
COLS, ROWS = 7, 6
# A start must name THIS game. A bare "start" used to match here too, but every game listener shares
# the one bot identity, so a bare "start" (e.g. "start #hangman") fired ALL of them at once — spawning
# phantom games. The app always posts the specific #connect4 tag, so require a connect4-specific token.
_START_RE = re.compile(r"\b(connect\s*4|connect\s*four|connectfour|c4)\b", re.IGNORECASE)
# An app-embedded game pointer inside a DM ("g:<64-hex-root> <move>"); bare human DM replies omit it
# and fall back to the per-player pending-game pointer.
_DM_GAME_RE = re.compile(r"\bg:([0-9a-f]{64})\b", re.IGNORECASE)
_COL_RE = re.compile(r"\b([1-7])\b")
_NOSTR_TOKEN_RE = re.compile(
    r"nostr:[a-z0-9]+|\b(?:npub1|nprofile1|nevent1|note1|naddr1)[023456789acdefghjklmnpqrstuvwxyz]+",
    re.IGNORECASE)
_LOOKBACK_DAYS = int(os.getenv("CONNECT4_LOOKBACK_DAYS", "3"))
_INVITE_MAX = int(os.getenv("CONNECT4_INVITE_MAX_PER_HOUR", "3"))
_INVITE_WINDOW = 3600
# Iterative-deepening alpha-beta bounded by a WALL-CLOCK budget: deepen 1,2,3… until the budget is
# spent, then play the best move from the last fully-searched depth. This caps the per-move compute
# (a fixed depth-5 pure-Python search rescanned the whole board every node and could peg a core for
# seconds — many concurrent games then starved the box). _MAX_DEPTH is just a ceiling now.
_MAX_DEPTH = max(1, int(os.getenv("CONNECT4_BOT_DEPTH", "7")))
_MOVE_BUDGET_S = max(0.05, float(os.getenv("CONNECT4_MOVE_BUDGET_S", "0.35")))
_invite_times: dict = {}
_DIRS = ((0, 1), (1, 0), (1, 1), (1, -1))


def _suffix():
    return hashlib.sha1((NOSTR_NSEC or "").encode()).hexdigest()[:10] if NOSTR_NSEC else "default"


_IDS_FILE = os.path.join(script_dir, f".processed_c4_ids_{_suffix()}")
_LOCK_FILE = _IDS_FILE + ".lock"
_DM_IDS_FILE = os.path.join(script_dir, f".processed_connect4_dms_{_suffix()}")
_MAX_IDS = 5000


def _claim_in(ids_file, item_id):
    lock_file = ids_file + ".lock"
    try:
        with open(lock_file, "w") as lk:
            fcntl.flock(lk.fileno(), fcntl.LOCK_EX)
            try:
                ids = set()
                try:
                    with open(ids_file) as f:
                        ids = {ln.strip() for ln in f if ln.strip()}
                except FileNotFoundError:
                    pass
                if item_id in ids:
                    return False
                ids.add(item_id)
                if len(ids) > _MAX_IDS:
                    ids = set(sorted(ids)[-_MAX_IDS:])
                fd, tmp = tempfile.mkstemp(dir=script_dir, prefix=".c4ids_")
                with os.fdopen(fd, "w") as f:
                    f.write("\n".join(ids))
                os.replace(tmp, ids_file)
                return True
            finally:
                fcntl.flock(lk.fileno(), fcntl.LOCK_UN)
    except Exception as e:
        print(f"[connect4] claim failed: {e}", flush=True)
        return False


def _claim_dm(rumor_id):
    return _claim_in(_DM_IDS_FILE, rumor_id)


def _claim(note_id):
    try:
        with open(_LOCK_FILE, "w") as lk:
            fcntl.flock(lk.fileno(), fcntl.LOCK_EX)
            try:
                ids = set()
                try:
                    with open(_IDS_FILE) as f:
                        ids = {ln.strip() for ln in f if ln.strip()}
                except FileNotFoundError:
                    pass
                if note_id in ids:
                    return False
                ids.add(note_id)
                if len(ids) > 5000:
                    ids = set(sorted(ids)[-5000:])
                fd, tmp = tempfile.mkstemp(dir=script_dir, prefix=".c4ids_")
                with os.fdopen(fd, "w") as f:
                    f.write("\n".join(ids))
                os.replace(tmp, _IDS_FILE)
                return True
            finally:
                fcntl.flock(lk.fileno(), fcntl.LOCK_UN)
    except Exception as e:
        print(f"[connect4] claim failed: {e}", flush=True)
        return False


# ---- state ----------------------------------------------------------------
def _dtag(gameid):
    return f"pcai:connect4:{gameid}"


def _save_game(gameid, state):
    ev = _ev.build_event(_nk._SECKEY, _KIND_APP, json.dumps(state, separators=(",", ":")),
                         tags=[["d", _dtag(gameid)]])
    _nk._run(_nk._svc.relay.publish(_nk._RELAYS, ev))


def _load_game(gameid):
    return _load_doc(_dtag(gameid))


def _load_doc(dtag):
    try:
        evs = _nk._run(_nk._svc.relay.query(
            _nk._RELAYS, [{"authors": [_nk._PUBKEY], "kinds": [_KIND_APP], "#d": [dtag], "limit": 1}])) or []
    except Exception:
        return None
    if not evs:
        return None
    evs.sort(key=lambda e: e.get("created_at", 0), reverse=True)
    try:
        return json.loads(evs[0].get("content") or "{}")
    except Exception:
        return None


# Per-player pointer to their current game — lets a new game supersede a player's unfinished one
# (one active game per user; newest wins). Keyed by pubkey.
def _player_dtag(pk):
    return f"pcai:connect4:player:{pk}"


def _get_player_game(pk):
    doc = _load_doc(_player_dtag(pk))
    return doc.get("gameid") if isinstance(doc, dict) else None


def _set_player_game(pk, gameid):
    ev = _ev.build_event(_nk._SECKEY, _KIND_APP, json.dumps({"gameid": gameid}, separators=(",", ":")),
                         tags=[["d", _player_dtag(pk)]])
    _nk._run(_nk._svc.relay.publish(_nk._RELAYS, ev))


# ---- nostr helpers --------------------------------------------------------
def _tags(note):
    return (note.get("_event") or {}).get("tags", [])


def _root_id(note):
    es = [t for t in _tags(note) if len(t) >= 2 and t[0] == "e"]
    for t in es:
        if len(t) >= 4 and t[3] == "root":
            return t[1]
    return es[0][1] if es else None


def _ptags(note):
    return [t[1] for t in _tags(note) if len(t) >= 2 and t[0] == "p" and t[1]]


def _name(pk):
    try:
        return "@" + (_nk.resolve_user(pk).get("username") or _nk._short_npub(pk))
    except Exception:
        return "@" + (pk or "")[:8]


def _clean_text(note):
    t = _NOSTR_TOKEN_RE.sub("", note.get("text") or "")
    t = re.sub(r"@[\w@.]+", "", t)
    t = re.sub(r"#\w+", "", t)
    for line in t.splitlines():
        if line.strip():
            return line.strip()
    return t.strip()


def _clean_dm_text(text):
    """Parse a move DM → (gameid_or_None, move_text). Strips the app's 'g:<root>' marker, nostr
    tokens and hashtags; the move is the first non-empty line."""
    m = _DM_GAME_RE.search(text or "")
    gameid = m.group(1).lower() if m else None
    t = _DM_GAME_RE.sub("", text or "")
    t = _NOSTR_TOKEN_RE.sub("", t)
    t = re.sub(r"#\w+", "", t)
    for line in t.splitlines():
        if line.strip():
            return gameid, line.strip()
    return gameid, t.strip()


def _footer():
    site = (os.getenv("CHESS_SITE_URL", "") or "").strip()
    play = f"\nPlay interactively at {site}." if site else ""
    return ("🔴 Wanna start your own game? Mention me with \"start @friend\" to challenge them "
            "(or just \"start\" to play me); I'll DM each of you the board to make your moves." + play
            + "\n#connect4 #nostr #gamestr")


def _publish(gameid, parent_id, p1, p2, body, png, federate=True):
    info = _nk._run(_nk._svc.media.upload(_nk._MEDIA_CFG, _nk._SECKEY, png, "image/png")) or {}
    url = info.get("url")
    if not url:
        raise RuntimeError("board image upload failed")
    # Real mentions, not bare @handles: a p-tag notifies but renders as plain text, so a result
    # post read "@npub1mq3s439… wins" — unrendered AND truncated. See _nk.mentionify.
    content = _nk.mentionify(f"{body}\n{url}\n\n{_footer()}", [p1, p2], _name)
    tags = [["e", gameid, "", "root"]]
    if parent_id and parent_id != gameid:
        tags.append(["e", parent_id, "", "reply"])
    for pk in (p1, p2):
        if pk:
            tags.append(["p", pk])
    for _t in ("connect4", "nostr", "gamestr"):
        tags.append(["t", _t])
    if not federate:
        tags.append(["nofederate", "1"])
    tags.append(_ev.imeta_tag(url, "image/png", info.get("sha256", ""), info.get("dim", "")))
    ev = _ev.build_event(_nk._SECKEY, 1, content, tags=tags)
    _nk._run(_nk._svc.relay.publish(_nk._RELAYS, ev))
    return ev


def _reply_text(note, text):
    try:
        _nk.send_reply(note, text + "\n\n#connect4 #nostr #gamestr")
    except Exception as e:
        print(f"[connect4] reply failed: {e}", flush=True)


# ---- game logic -----------------------------------------------------------
def _drop_row(cells, col):
    for r in range(ROWS - 1, -1, -1):
        if not cells[r * COLS + col]:
            return r
    return None


def _winner(cells):
    for r in range(ROWS):
        for c in range(COLS):
            p = cells[r * COLS + c]
            if not p:
                continue
            for dr, dc in _DIRS:
                if all(0 <= r + dr * k < ROWS and 0 <= c + dc * k < COLS
                       and cells[(r + dr * k) * COLS + (c + dc * k)] == p for k in range(4)):
                    return p
    return None


def _side_to_move(cells):
    return "1" if sum(1 for c in cells if c) % 2 == 0 else "2"


def _status_after(cells):
    w = _winner(cells)
    if w:
        return "win", w
    if all(cells):
        return "draw", None
    return "active", None


class _SearchTimeout(Exception):
    """Raised deep in the search to abort the current (unfinished) depth when the budget is spent."""


def _bot_move(cells, mark):
    """Iterative-deepening alpha-beta with a wall-clock budget + window-scoring eval. Deepens
    1,2,3…_MAX_DEPTH, keeping the best move from the last FULLY-searched depth, and aborts the
    moment _MOVE_BUDGET_S is spent — so a single move can never peg a core, no matter the position
    or how many games are in flight. Returns the best column 0..6 (or None if the board is full)."""
    opp = "2" if mark == "1" else "1"
    deadline = time.monotonic() + _MOVE_BUDGET_S
    nodes = [0]

    def win_score(win):
        m, o, e = win.count(mark), win.count(opp), win.count("")
        if m and o:
            return 0
        if m == 3 and e == 1:
            return 50
        if m == 2 and e == 2:
            return 10
        if o == 3 and e == 1:
            return -80
        if o == 2 and e == 2:
            return -8
        return 0

    def evaluate(cs):
        s = 6 * (sum(1 for r in range(ROWS) if cs[r * COLS + 3] == mark)
                 - sum(1 for r in range(ROWS) if cs[r * COLS + 3] == opp))
        for r in range(ROWS):
            for c in range(COLS):
                for dr, dc in _DIRS:
                    if 0 <= r + dr * 3 < ROWS and 0 <= c + dc * 3 < COLS:
                        s += win_score([cs[(r + dr * k) * COLS + (c + dc * k)] for k in range(4)])
        return s

    def nm(cs, depth, alpha, beta, turn, root_depth):
        nodes[0] += 1
        if (nodes[0] & 2047) == 0 and time.monotonic() > deadline:   # cheap periodic time check
            raise _SearchTimeout
        w = _winner(cs)
        if w == mark:
            return 1_000_000 - (root_depth - depth)   # prefer FASTER wins
        if w == opp:
            return -1_000_000 + (root_depth - depth)  # delay losses
        valid = [c for c in range(COLS) if _drop_row(cs, c) is not None]
        if not valid or depth == 0:
            return evaluate(cs)
        order = [c for c in (3, 2, 4, 1, 5, 0, 6) if c in valid]
        if turn == mark:
            best = -10_000_000
            for c in order:
                r = _drop_row(cs, c); cs[r * COLS + c] = mark
                best = max(best, nm(cs, depth - 1, alpha, beta, opp, root_depth)); cs[r * COLS + c] = ""
                alpha = max(alpha, best)
                if alpha >= beta:
                    break
            return best
        else:
            best = 10_000_000
            for c in order:
                r = _drop_row(cs, c); cs[r * COLS + c] = opp
                best = min(best, nm(cs, depth - 1, alpha, beta, mark, root_depth)); cs[r * COLS + c] = ""
                beta = min(beta, best)
                if beta <= alpha:
                    break
            return best

    valid = [c for c in (3, 2, 4, 1, 5, 0, 6) if _drop_row(cells, c) is not None]
    if not valid:
        return None
    best_c = valid[0]
    work = list(cells)
    for d in range(1, _MAX_DEPTH + 1):           # iterative deepening
        cur_c, cur_v = None, -10_000_001
        try:
            for c in valid:
                r = _drop_row(work, c); work[r * COLS + c] = mark
                v = nm(work, d - 1, -10_000_000, 10_000_000, opp, d); work[r * COLS + c] = ""
                if v > cur_v:
                    cur_v, cur_c = v, c
        except _SearchTimeout:
            for i, ch in enumerate(work):        # undo any in-flight trial move, then keep prior best
                work[i] = cells[i]
            break
        if cur_c is not None:
            best_c = cur_c
        if cur_v >= 1_000_000 or time.monotonic() > deadline:   # forced win found, or out of time
            break
    return best_c


def _apply_bot(state):
    last = None
    # Self-game guard (p1 == p2): never auto-play both sides — that pegs a core (see chess).
    if state.get("p1") == state.get("p2"):
        return None
    while state.get("status") == "active":
        stm = _side_to_move(state["cells"])
        side_pk = state["p1"] if stm == "1" else state["p2"]
        if side_pk != _nk._PUBKEY:
            break
        col = _bot_move(state["cells"], stm)
        if col is None:
            break
        r = _drop_row(state["cells"], col)
        if r is None:
            break
        state["cells"][r * COLS + col] = stm
        last = r * COLS + col
        st, w = _status_after(state["cells"])
        if st != "active":
            state["status"] = st
            break
    return last


# ---- posts ----------------------------------------------------------------
def _dm_current_player(state, gameid):
    """DM the board to the side now to move (private gameplay). No-op if it's the bot's turn.
    Sets the per-player pending-game pointer so a bare DM reply (no app marker) routes back here."""
    cells = state["cells"]
    stm = _side_to_move(cells)
    mover_pk = state["p1"] if stm == "1" else state["p2"]
    if not mover_pk or mover_pk == _nk._PUBKEY:
        return
    mover_white = stm == "1"
    opp_nm = state["p2_name"] if mover_white else state["p1_name"]
    colour = "cyan" if mover_white else "magenta"
    title = f"YOUR MOVE ({colour})"
    sub = f"{state['p1_name']} = cyan   ·   {state['p2_name']} = magenta"
    png = connect4_render.render(cells, last_move=state.get("last_move"), title=title, subtitle=sub)
    try:
        info = _nk._run(_nk._svc.media.upload(_nk._MEDIA_CFG, _nk._SECKEY, png, "image/png")) or {}
        url = info.get("url") or ""
    except Exception as e:
        print(f"[connect4] DM board upload failed: {e}", flush=True)
        url = ""
    body = (f"🔴 Your move vs {opp_nm}\nYou're {colour}.\n"
            + (url + "\n\n" if url else "")
            + "Reply with a column number 1-7 to drop your disc (or 'resign'). "
              "Or play from the Connect Four tab in the app.")
    try:
        _nk.send_dm(mover_pk, body, extra_tags=[["g", gameid]])
        _set_player_game(mover_pk, gameid)
    except Exception as e:
        print(f"[connect4] send_dm failed: {e}", flush=True)


def _post_active(state, gameid, parent_id, last):
    cells = state["cells"]
    state["last_move"] = last
    # MID-GAME: no public post — just persist + DM the next player their board.
    if any(cells):
        _save_game(gameid, state)
        _dm_current_player(state, gameid)
        return
    # OPENING: one public invitation post, then DM the first player their board.
    stm = _side_to_move(cells)
    mover_nm = state["p1_name"] if stm == "1" else state["p2_name"]
    colour = "cyan" if stm == "1" else "magenta"
    title = f"{mover_nm} to move ({colour})"
    sub = f"{state['p1_name']} = cyan   ·   {state['p2_name']} = magenta"
    png = connect4_render.render(cells, last_move=last, title=title, subtitle=sub)
    vs_bot = _nk._PUBKEY in (state["p1"], state["p2"])
    if vs_bot:
        human_p1 = state["p1"] != _nk._PUBKEY
        body = (f"🔴 #connect4 vs the bot — you're {'cyan' if human_p1 else 'magenta'}.\n"
                f"📩 Check your DMs — I've sent you the board there, and the whole game plays out "
                f"privately in DMs. The result gets posted here.")
    else:
        body = (f"🔴 #connect4 — {state['p1_name']} (cyan) vs {state['p2_name']} (magenta)!\n"
                f"📩 {mover_nm}, you're up — check your DMs to drop the first disc. The game plays out "
                f"privately in DMs; I'll post the result here when it's over.")
    ev = _publish(gameid, parent_id, state["p1"], state["p2"], body, png, federate=True)
    state["last_board_event"] = ev.get("id")
    _save_game(gameid, state)
    _dm_current_player(state, gameid)


def _post_over(state, gameid, parent_id, last, result_text, winner_pk="__auto__"):
    # winner_pk: a player's pubkey on a win, None on a draw. "__auto__" → resolve from the board.
    if winner_pk == "__auto__":
        w = _winner(state["cells"])
        winner_pk = (state["p1"] if w == "1" else state["p2"]) if w else None
    state["result"] = result_text
    state["winner_pk"] = winner_pk
    state["winner_name"] = (state["p1_name"] if winner_pk == state["p1"]
                            else state["p2_name"] if winner_pk == state["p2"] else None)
    png = connect4_render.render(state["cells"], last_move=last, title="GAME OVER", subtitle=result_text)
    _publish(gameid, parent_id, state["p1"], state["p2"], f"🏁 {result_text}  gg!", png)
    _save_game(gameid, state)


# ---- start + move ---------------------------------------------------------
def _start_game(note, own_pk):
    sender = (note.get("user") or {}).get("pubkey")
    opponents = [p for p in _ptags(note) if p and p != own_pk and p != sender]
    gameid = note["id"]
    if _load_game(gameid):
        return
    now = time.time()
    recent = [t for t in _invite_times.get(sender, []) if now - t < _INVITE_WINDOW]
    if _INVITE_MAX and len(recent) >= _INVITE_MAX:
        _reply_text(note, f"⏳ You've started {_INVITE_MAX} games in the last hour — that's the limit.")
        return
    recent.append(now)
    _invite_times[sender] = recent
    # player 1 (cyan) moves first. Invited opponent is p1 (accepts by moving); vs-bot → human is p1.
    if opponents:
        p1, p2 = opponents[0], sender
    else:
        p1, p2 = sender, own_pk
    if not p1 or p1 == p2:   # never create a self-game (would peg a core auto-playing both sides)
        print(f"[connect4] skip self/invalid game (p1==p2) for {gameid[:12]}", flush=True)
        return
    state = {
        "v": 1, "p1": p1, "p2": p2, "p1_name": _name(p1), "p2_name": _name(p2),
        "cells": [""] * (ROWS * COLS), "status": "active", "root": gameid,
        "started": int(time.time()), "last_board_event": None,
    }
    print(f"[connect4] new game {gameid[:12]} {state['p1_name']} vs {state['p2_name']}", flush=True)
    if state["p1"] == own_pk:
        _apply_bot(state)
    _post_active(state, gameid, gameid, None)


def _apply_move(sender, gameid, state, text, reply, parent_id):
    """Apply one move from `sender`. `reply(msg)` sends a nudge/error on the same channel (public
    reply or DM). Game-over posts go to `parent_id` (public). Mid-game = no public post; the next
    player is DM'd by _post_active."""
    if sender not in (state["p1"], state["p2"]):
        return
    if text.lower() in ("resign", "quit", "gg", "abandon", "/resign"):
        winner_pk = state["p2"] if sender == state["p1"] else state["p1"]
        winner = state["p2_name"] if sender == state["p1"] else state["p1_name"]
        state["status"] = "resigned"
        _post_over(state, gameid, parent_id, None, f"{_name(sender)} resigned. {winner} wins!",
                   winner_pk=winner_pk)
        return
    if state.get("status") != "active":
        reply("🏁 This game is over. Start a new one with \"start @opponent\".")
        return
    stm = _side_to_move(state["cells"])
    side_pk = state["p1"] if stm == "1" else state["p2"]
    if sender != side_pk:
        reply("⏳ It's not your turn.")
        return
    m = _COL_RE.search(text)
    if not m:
        reply("🤔 Reply with a column number 1-7.")
        return
    col = int(m.group(1)) - 1
    r = _drop_row(state["cells"], col)
    if r is None:
        reply(f"🚫 Column {col + 1} is full. Pick another.")
        return
    state["cells"][r * COLS + col] = stm
    last = r * COLS + col
    st, w = _status_after(state["cells"])
    if st != "active":
        state["status"] = st
        result = (f"{state['p1_name'] if w == '1' else state['p2_name']} "
                  f"({'cyan' if w == '1' else 'magenta'}) wins!" if w else "Draw — board full.")
        win_pk = (state["p1"] if w == "1" else state["p2"]) if w else None
        _post_over(state, gameid, parent_id, last, result, winner_pk=win_pk)
        return
    nstm = _side_to_move(state["cells"])
    next_pk = state["p1"] if nstm == "1" else state["p2"]
    if next_pk == _nk._PUBKEY:
        blast = _apply_bot(state)
        if state.get("status") != "active":
            _, w2 = _status_after(state["cells"])
            result = (f"{state['p1_name'] if w2 == '1' else state['p2_name']} "
                      f"({'cyan' if w2 == '1' else 'magenta'}) wins!" if w2 else "Draw — board full.")
            win_pk2 = (state["p1"] if w2 == "1" else state["p2"]) if w2 else None
            _post_over(state, gameid, parent_id, blast, result, winner_pk=win_pk2)
        else:
            _post_active(state, gameid, parent_id, blast)
    else:
        _post_active(state, gameid, parent_id, last)


def _handle_move(note, gameid, state):
    """Public-reply move path (cross-client public play still works)."""
    sender = (note.get("user") or {}).get("pubkey")
    _apply_move(sender, gameid, state, _clean_text(note), lambda m: _reply_text(note, m), note["id"])


def _handle_dm(sender, gameid, state, move_text):
    """Private-DM move path — nudges/errors go back as DMs."""
    _apply_move(sender, gameid, state, move_text,
                lambda m: _nk.send_dm(sender, m), state.get("last_board_event") or gameid)


def process_connect4():
    own = _nk.get_own_account()
    if not own:
        print("[connect4] no account (NOSTR_NSEC missing) — idle", flush=True)
        return
    own_pk = own.get("pubkey")
    cutoff = int(time.time()) - _LOOKBACK_DAYS * 86400
    for note in _nk.get_mentions(limit=40):
        nid = note.get("id")
        if not nid or (note.get("user") or {}).get("pubkey") == own_pk:
            continue
        if (note.get("_event") or {}).get("created_at", 0) < cutoff:
            continue
        root = _root_id(note)
        state = _load_game(root) if root else None
        is_start = bool(_START_RE.search(note.get("text") or "")) and not state
        if not state and not is_start:
            continue
        if not _claim(nid):
            continue
        try:
            if state:
                _handle_move(note, root, state)
            else:
                _start_game(note, own_pk)
        except Exception as e:
            print(f"[connect4] processing {nid[:12]} failed: {e}", flush=True)
            import traceback
            traceback.print_exc()
    # ---- private gameplay: read move DMs (NIP-17) ----------------------------
    try:
        dms = _nk.read_dms(limit=100)
    except Exception as e:
        print(f"[connect4] read_dms failed: {e}", flush=True)
        dms = []
    for dm in dms:
        rid = dm.get("rumor_id")
        sender = dm.get("sender")
        if not rid or not sender or sender == own_pk:
            continue
        if dm.get("created_at", 0) < cutoff:
            continue
        gameid, move_text = _clean_dm_text(dm.get("text") or "")
        if not move_text:
            continue
        if not gameid:
            gameid = _get_player_game(sender)   # bare DM reply → the player's pending game
        if not gameid:
            continue
        if not _claim_dm(rid):
            continue
        state = _load_game(gameid)
        if not state:
            continue
        try:
            _handle_dm(sender, gameid, state, move_text)
        except Exception as e:
            print(f"[connect4] DM move {rid[:12]} failed: {e}", flush=True)
            import traceback
            traceback.print_exc()
