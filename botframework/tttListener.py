"""#tictactoe — play Tic-Tac-Toe with another Nostr user (or the bot), refereed by the bot.

Mirrors the #chesstr bot: START by posting "tictactoe @opponent" (or "ttt @opponent") mentioning the
bot — the invited player is X and moves first; with no opponent you play the bot (you're X, bot is O).
MOVE by replying with a cell number 1-9 (top-left → bottom-right). The bot validates, renders a
cyberpunk board, posts it tagging the other player, and calls the win/draw. State is a replaceable
kind-30078 doc keyed by the game root id, so games never expire. Every post carries #tictactoe.
"""
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

import ttt_render
import nostr as _nk
from config import NOSTR_NSEC
from app.services.nostr import event as _ev

_KIND_APP = 30078
_START_RE = re.compile(r"\b(tic\s*tac\s*toe|tictactoe|ttt)\b", re.IGNORECASE)
_CELL_RE = re.compile(r"\b([1-9])\b")
_NOSTR_TOKEN_RE = re.compile(
    r"nostr:[a-z0-9]+|\b(?:npub1|nprofile1|nevent1|note1|naddr1)[023456789acdefghjklmnpqrstuvwxyz]+",
    re.IGNORECASE)
_LOOKBACK_DAYS = int(os.getenv("TTT_LOOKBACK_DAYS", "3"))
_INVITE_MAX = int(os.getenv("TTT_INVITE_MAX_PER_HOUR", "3"))
_INVITE_WINDOW = 3600
_invite_times: dict = {}
_WINS = [(0, 1, 2), (3, 4, 5), (6, 7, 8), (0, 3, 6), (1, 4, 7), (2, 5, 8), (0, 4, 8), (2, 4, 6)]


# ---- cross-restart dedup --------------------------------------------------
def _suffix():
    return hashlib.sha1((NOSTR_NSEC or "").encode()).hexdigest()[:10] if NOSTR_NSEC else "default"


_IDS_FILE = os.path.join(script_dir, f".processed_ttt_ids_{_suffix()}")
_LOCK_FILE = _IDS_FILE + ".lock"


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
                fd, tmp = tempfile.mkstemp(dir=script_dir, prefix=".tttids_")
                with os.fdopen(fd, "w") as f:
                    f.write("\n".join(ids))
                os.replace(tmp, _IDS_FILE)
                return True
            finally:
                fcntl.flock(lk.fileno(), fcntl.LOCK_UN)
    except Exception as e:
        print(f"[ttt] claim failed: {e}", flush=True)
        return False


# ---- state store (kind-30078) ---------------------------------------------
def _dtag(gameid):
    return f"pcai:ttt:{gameid}"


def _save_game(gameid, state):
    ev = _ev.build_event(_nk._SECKEY, _KIND_APP, json.dumps(state, separators=(",", ":")),
                         tags=[["d", _dtag(gameid)]])
    _nk._run(_nk._svc.relay.publish(_nk._RELAYS, ev))


def _load_game(gameid):
    try:
        evs = _nk._run(_nk._svc.relay.query(
            _nk._RELAYS, [{"authors": [_nk._PUBKEY], "kinds": [_KIND_APP], "#d": [_dtag(gameid)], "limit": 1}])) or []
    except Exception:
        return None
    if not evs:
        return None
    evs.sort(key=lambda e: e.get("created_at", 0), reverse=True)
    try:
        return json.loads(evs[0].get("content") or "{}")
    except Exception:
        return None


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


def _footer():
    site = (os.getenv("CHESS_SITE_URL", "") or "").strip()
    play = f"\nPlay interactively at {site}." if site else ""
    return ("⭕ Wanna start your own game? Reply \"tictactoe @friend\" to challenge them "
            "(or just \"tictactoe\" to play me); then reply with a cell number 1-9." + play + "\n#tictactoe #nostr #gamestr")


def _publish(gameid, parent_id, px, po, body, png, federate=True):
    info = _nk._run(_nk._svc.media.upload(_nk._MEDIA_CFG, _nk._SECKEY, png, "image/png")) or {}
    url = info.get("url")
    if not url:
        raise RuntimeError("board image upload failed")
    content = f"{body}\n{url}\n\n{_footer()}"
    tags = [["e", gameid, "", "root"]]
    if parent_id and parent_id != gameid:
        tags.append(["e", parent_id, "", "reply"])
    for pk in (px, po):
        if pk:
            tags.append(["p", pk])
    for _t in ("tictactoe", "nostr", "gamestr"):
        tags.append(["t", _t])
    if not federate:   # mid-game boards stay local-only (only opening + final go public)
        tags.append(["nofederate", "1"])
    tags.append(_ev.imeta_tag(url, "image/png", info.get("sha256", ""), info.get("dim", "")))
    ev = _ev.build_event(_nk._SECKEY, 1, content, tags=tags)
    _nk._run(_nk._svc.relay.publish(_nk._RELAYS, ev))
    return ev


def _reply_text(note, text):
    try:
        _nk.send_reply(note, text + "\n\n#tictactoe #nostr #gamestr")
    except Exception as e:
        print(f"[ttt] reply failed: {e}", flush=True)


# ---- game logic -----------------------------------------------------------
def _winner(cells):
    for a, b, c in _WINS:
        if cells[a] and cells[a] == cells[b] == cells[c]:
            return cells[a]
    return None


def _side_to_move(cells):
    return "X" if sum(1 for c in cells if c) % 2 == 0 else "O"


def _bot_move(cells, mark):
    """Perfect play (minimax). mark = the bot's symbol. Returns a cell index 0..8."""
    opp = "O" if mark == "X" else "X"

    def score(cs, depth):
        w = _winner(cs)
        if w == mark:
            return 10 - depth
        if w == opp:
            return depth - 10
        if all(cs):
            return 0
        return None

    def minimax(cs, turn, depth):
        s = score(cs, depth)
        if s is not None:
            return s, None
        best, best_i = (-99, None) if turn == mark else (99, None)
        for i in range(9):
            if cs[i]:
                continue
            cs[i] = turn
            val, _ = minimax(cs, opp if turn == mark else mark, depth + 1)
            cs[i] = ""
            if turn == mark and val > best:
                best, best_i = val, i
            elif turn != mark and val < best:
                best, best_i = val, i
        return best, best_i

    _, i = minimax(list(cells), mark, 0)
    return i


def _status_after(cells):
    w = _winner(cells)
    if w:
        return "win", w
    if all(cells):
        return "draw", None
    return "active", None


def _post_active(state, gameid, parent_id, last):
    cells = state["cells"]
    # MID-GAME: state-only, NO public post (the app renders from state). Only the opening + final post.
    if any(cells):
        _save_game(gameid, state)
        return
    stm = _side_to_move(cells)
    mover_pk = state["x"] if stm == "X" else state["o"]
    mover_nm = state["x_name"] if stm == "X" else state["o_name"]
    title = f"{mover_nm} ({stm}) to move"
    sub = f"{state['x_name']} = X (cyan)   ·   {state['o_name']} = O (magenta)"
    png = ttt_render.render_board(cells, last_move=last, title=title, subtitle=sub)
    if not any(cells):
        vs_bot = _nk._PUBKEY in (state["x"], state["o"])
        body = (f"⭕ #tictactoe — {state['x_name']} (X) vs {state['o_name']} (O)!\n"
                f"{mover_nm}, you're up ({stm}). Reply with a cell number 1-9 (top-left → bottom-right), "
                f"or tap on the Games → Tic-Tac-Toe tab.")
        if vs_bot:
            body = (f"🤖 #tictactoe vs the bot — you're {('X' if state['x'] != _nk._PUBKEY else 'O')}.\n"
                    f"Reply with a cell number 1-9, or tap on the Games tab.")
    else:
        body = f"⭕ {('Cell ' + str(last + 1) + '. ') if last is not None else ''}{mover_nm} ({stm}) to move — reply 1-9."
    # Opening (empty board) is public; mid-game move boards stay local-only (anti-spam).
    ev = _publish(gameid, parent_id, state["x"], state["o"], body, png, federate=(not any(cells)))
    state["last_board_event"] = ev.get("id")
    _save_game(gameid, state)


def _post_over(state, gameid, parent_id, last, result_text):
    png = ttt_render.render_board(state["cells"], last_move=last, title="GAME OVER", subtitle=result_text)
    _publish(gameid, parent_id, state["x"], state["o"], f"🏁 {result_text}  gg!", png)
    _save_game(gameid, state)


def _apply_bot(state):
    """If it's the bot's turn, play perfect move(s). Mutates state. Returns last cell played."""
    last = None
    while state.get("status") == "active":
        stm = _side_to_move(state["cells"])
        side_pk = state["x"] if stm == "X" else state["o"]
        if side_pk != _nk._PUBKEY:
            break
        i = _bot_move(state["cells"], stm)
        if i is None:
            break
        state["cells"][i] = stm
        last = i
        st, w = _status_after(state["cells"])
        if st != "active":
            state["status"] = st
            break
    return last


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
    # X moves first. Invited opponent is X (accepts by moving); vs-bot → human is X, bot is O.
    if opponents:
        x, o = opponents[0], sender
    else:
        x, o = sender, own_pk
    state = {
        "v": 1, "x": x, "o": o, "x_name": _name(x), "o_name": _name(o),
        "cells": [""] * 9, "status": "active", "root": gameid,
        "started": int(time.time()), "last_board_event": None,
    }
    print(f"[ttt] new game {gameid[:12]} {state['x_name']} vs {state['o_name']}", flush=True)
    # bot is X (moves first)? play its opening.
    if state["x"] == own_pk:
        _apply_bot(state)
    _post_active(state, gameid, gameid, None)


def _handle_move(note, gameid, state):
    sender = (note.get("user") or {}).get("pubkey")
    if sender not in (state["x"], state["o"]):
        return
    text = _clean_text(note)
    if text.lower() in ("resign", "quit", "gg", "abandon", "/resign"):
        winner = state["o_name"] if sender == state["x"] else state["x_name"]
        state["status"] = "resigned"
        _post_over(state, gameid, note["id"], None, f"{_name(sender)} resigned. {winner} wins!")
        return
    if state.get("status") != "active":
        _reply_text(note, "🏁 This game is over. Start a new one with \"tictactoe @opponent\".")
        return
    stm = _side_to_move(state["cells"])
    side_pk = state["x"] if stm == "X" else state["o"]
    if sender != side_pk:
        _reply_text(note, "⏳ It's not your turn.")
        return
    m = _CELL_RE.search(text)
    if not m:
        _reply_text(note, "🤔 Reply with a cell number 1-9 (top-left → bottom-right).")
        return
    i = int(m.group(1)) - 1
    if state["cells"][i]:
        _reply_text(note, f"🚫 Cell {i + 1} is taken. Pick an empty one.")
        return
    state["cells"][i] = stm
    last = i
    st, w = _status_after(state["cells"])
    if st != "active":
        state["status"] = st
        result = (f"{state['x_name'] if w == 'X' else state['o_name']} ({w}) wins!" if w else "Draw — cat's game.")
        _post_over(state, gameid, note["id"], last, result)
        return
    # vs bot?
    nstm = _side_to_move(state["cells"])
    next_pk = state["x"] if nstm == "X" else state["o"]
    if next_pk == _nk._PUBKEY:
        blast = _apply_bot(state)
        if state.get("status") != "active":
            _, w2 = _status_after(state["cells"])
            result = (f"{state['x_name'] if w2 == 'X' else state['o_name']} ({w2}) wins!" if w2 else "Draw — cat's game.")
            _post_over(state, gameid, note["id"], blast, result)
        else:
            _post_active(state, gameid, note["id"], blast)
    else:
        _post_active(state, gameid, note["id"], last)


def process_ttt():
    own = _nk.get_own_account()
    if not own:
        print("[ttt] no account (NOSTR_NSEC missing) — idle", flush=True)
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
            print(f"[ttt] processing {nid[:12]} failed: {e}", flush=True)
            import traceback
            traceback.print_exc()
