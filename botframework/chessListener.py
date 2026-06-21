"""#chesstr — play chess with another user over Nostr (the bot is the referee + board).

Flow:
  • START: someone posts mentioning the bot + another user with the word "chess"
    (e.g. "@chessbot chess @bob"). The initiator is WHITE (cyan), the opponent BLACK (magenta).
    The bot replies with the cyberpunk board, the side-to-move's pieces NUMBERED, tagging both.
  • MOVE: a player replies to the bot's board post with "<n> <square>" (e.g. "1 d4" — move the
    piece labelled 1 to d4). SAN ("Nf3"), UCI ("g1f3"), "O-O"/"O-O-O" and "resign" also work.
    The bot validates, applies it, and posts the updated board tagging the other player.
  • GAME OVER: checkmate / stalemate / draw / resign → a final board + result post.

Every post carries the #chesstr hashtag (text + a `t` tag). Game state is stored as a replaceable
kind-30078 app-data event keyed by the game's root note id, so games survive restarts and never
expire — they just wait for the next reply, however many days later.
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

import chess
import chess_render
import nostr as _nk
from config import NOSTR_NSEC
from app.services.nostr import event as _ev

_KIND_APP = 30078
_START_RE = re.compile(r"\bchess(tr)?\b", re.IGNORECASE)
# "<n> <square>" with an optional separator: "1 d4", "1->d4", "12e5"
_MOVE_RE = re.compile(r"\b(\d{1,2})\s*(?:->|-|to|\.)?\s*([a-h][1-8])\b", re.IGNORECASE)
_NOSTR_TOKEN_RE = re.compile(
    r"nostr:[a-z0-9]+|\b(?:npub1|nprofile1|nevent1|note1|naddr1)[023456789acdefghjklmnpqrstuvwxyz]+",
    re.IGNORECASE,
)
_POLL_LOOKBACK_DAYS = int(os.getenv("CHESS_LOOKBACK_DAYS", "3"))


# ---- cross-restart dedup (one claim per processed note id) -------------------
def _suffix() -> str:
    return hashlib.sha1((NOSTR_NSEC or "").encode()).hexdigest()[:10] if NOSTR_NSEC else "default"


_IDS_FILE = os.path.join(script_dir, f".processed_chess_ids_{_suffix()}")
_LOCK_FILE = _IDS_FILE + ".lock"
_MAX_IDS = 5000


def _claim(note_id: str) -> bool:
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
                if len(ids) > _MAX_IDS:
                    ids = set(sorted(ids)[-_MAX_IDS:])
                fd, tmp = tempfile.mkstemp(dir=script_dir, prefix=".chids_")
                with os.fdopen(fd, "w") as f:
                    f.write("\n".join(ids))
                os.replace(tmp, _IDS_FILE)
                return True
            finally:
                fcntl.flock(lk.fileno(), fcntl.LOCK_UN)
    except Exception as e:
        print(f"[chesstr] claim failed: {e}", flush=True)
        return False


# ---- game state store (replaceable kind-30078, keyed by game root id) --------
def _dtag(gameid: str) -> str:
    return f"pcai:chesstr:{gameid}"


def _save_game(gameid: str, state: dict):
    ev = _ev.build_event(_nk._SECKEY, _KIND_APP, json.dumps(state, separators=(",", ":")),
                         tags=[["d", _dtag(gameid)]])
    _nk._run(_nk._svc.relay.publish(_nk._RELAYS, ev))


def _load_game(gameid: str):
    try:
        evs = _nk._run(_nk._svc.relay.query(
            _nk._RELAYS, [{"authors": [_nk._PUBKEY], "kinds": [_KIND_APP],
                           "#d": [_dtag(gameid)], "limit": 1}])) or []
    except Exception:
        return None
    if not evs:
        return None
    evs.sort(key=lambda e: e.get("created_at", 0), reverse=True)
    try:
        return json.loads(evs[0].get("content") or "{}")
    except Exception:
        return None


# ---- nostr helpers -----------------------------------------------------------
def _tags(note):
    return (note.get("_event") or {}).get("tags", [])


def _root_id(note):
    """The NIP-10 root e-tag (game id), preferring a marked root, else the first e-tag."""
    es = [t for t in _tags(note) if len(t) >= 2 and t[0] == "e"]
    for t in es:
        if len(t) >= 4 and t[3] == "root":
            return t[1]
    return es[0][1] if es else None


def _ptags(note):
    return [t[1] for t in _tags(note) if len(t) >= 2 and t[0] == "p" and t[1]]


def _name(pk: str) -> str:
    try:
        return "@" + (_nk.resolve_user(pk).get("username") or _nk._short_npub(pk))
    except Exception:
        return "@" + (pk or "")[:8]


def _clean_text(note) -> str:
    t = _NOSTR_TOKEN_RE.sub("", note.get("text") or "")
    return re.sub(r"@[\w@.]+", "", t).strip()


def _publish(gameid, parent_id, white, black, body, png):
    """Post a board image as a reply (root=gameid), tagging both players + #chesstr."""
    info = _nk._run(_nk._svc.media.upload(_nk._MEDIA_CFG, _nk._SECKEY, png, "image/png")) or {}
    url = info.get("url")
    if not url:
        raise RuntimeError("board image upload failed")
    content = f"{body}\n{url}\n\n#chesstr"
    tags = [["e", gameid, "", "root"]]
    if parent_id and parent_id != gameid:
        tags.append(["e", parent_id, "", "reply"])
    for pk in (white, black):
        if pk:
            tags.append(["p", pk])
    tags.append(["t", "chesstr"])
    tags.append(_ev.imeta_tag(url, "image/png", info.get("sha256", ""), info.get("dim", "")))
    ev = _ev.build_event(_nk._SECKEY, 1, content, tags=tags)
    _nk._run(_nk._svc.relay.publish(_nk._RELAYS, ev))
    return ev


def _reply_text(parent_note, text):
    """A plain text reply (errors / nudges) — also #chesstr-tagged, in the game thread."""
    try:
        _nk.send_reply(parent_note, text + "\n\n#chesstr")
    except Exception as e:
        print(f"[chesstr] reply failed: {e}", flush=True)


# ---- move parsing ------------------------------------------------------------
def _parse_move(board: chess.Board, text: str):
    """Return a legal chess.Move or None. Tries numbered '<n> <sq>', then SAN, then UCI."""
    low = text.strip().lower()
    if low in ("o-o", "0-0", "castle", "castle short", "castle kingside"):
        for m in board.legal_moves:
            if board.is_kingside_castling(m):
                return m
    if low in ("o-o-o", "0-0-0", "castle long", "castle queenside"):
        for m in board.legal_moves:
            if board.is_queenside_castling(m):
                return m
    m = _MOVE_RE.search(text)
    if m:
        num = int(m.group(1))
        nums = chess_render.piece_numbers(board, board.turn)
        from_sq = nums.get(num)
        if from_sq is None:
            return "no_piece"
        to_sq = chess.parse_square(m.group(2).lower())
        cands = [mv for mv in board.legal_moves if mv.from_square == from_sq and mv.to_square == to_sq]
        if not cands:
            return "illegal"
        # prefer a queen promotion when the move is a promotion
        for mv in cands:
            if mv.promotion == chess.QUEEN:
                return mv
        return cands[0]
    # SAN ("Nf3", "exd5", "e8=Q")
    try:
        return board.parse_san(text.strip())
    except Exception:
        pass
    # UCI ("g1f3")
    try:
        mv = chess.Move.from_uci(low)
        if mv in board.legal_moves:
            return mv
    except Exception:
        pass
    return None


_DRAW_LABELS = {
    chess.Termination.STALEMATE: "Stalemate",
    chess.Termination.INSUFFICIENT_MATERIAL: "Insufficient material",
    chess.Termination.SEVENTYFIVE_MOVES: "75-move rule",
    chess.Termination.FIVEFOLD_REPETITION: "Fivefold repetition",
    chess.Termination.FIFTY_MOVES: "50-move rule",
    chess.Termination.THREEFOLD_REPETITION: "Threefold repetition",
}


def _status_for(board: chess.Board):
    out = board.outcome(claim_draw=True)
    if out is None:
        return "active", ""
    if out.winner is not None:
        return "checkmate", ("White wins by checkmate!" if out.winner == chess.WHITE else "Black wins by checkmate!")
    return "draw", f"½–½ {_DRAW_LABELS.get(out.termination, 'Draw')}."


def _post_active_board(state, gameid, parent_id, san):
    """Render + post the board for the side now to move (after a move or at game start)."""
    board = chess.Board(state["fen"])
    mover_white = board.turn == chess.WHITE
    mover_pk = state["white"] if mover_white else state["black"]
    mover_nm = state["white_name"] if mover_white else state["black_name"]
    move_no = board.fullmove_number
    chk = " — CHECK!" if board.is_check() else ""
    title = f"{'WHITE' if mover_white else 'BLACK'} to move{chk}"
    sub = f"{state['white_name']} (cyan) vs {state['black_name']} (magenta)  ·  move {move_no}"
    png = chess_render.render_board(state["fen"], last_move=state.get("last_move"),
                                    number_color=board.turn, title=title, subtitle=sub)
    last = f"Last move: {san}. " if san else ""
    body = (f"♟️ {last}{mover_nm} ({'cyan' if mover_white else 'magenta'}) to move{chk}\n"
            f"Reply to THIS post with your move, e.g. '1 d4' (move piece #1 to d4). "
            f"SAN/UCI/O-O also work.")
    ev = _publish(gameid, parent_id, state["white"], state["black"], body, png)
    state["last_board_event"] = ev.get("id")
    _save_game(gameid, state)


def _post_gameover(state, gameid, parent_id, san, result_text):
    board = chess.Board(state["fen"])
    png = chess_render.render_board(state["fen"], last_move=state.get("last_move"),
                                    number_color=None, title="GAME OVER", subtitle=result_text)
    body = (f"🏁 {('Last move: ' + san + '. ') if san else ''}{result_text}\n"
            f"{state['white_name']} (cyan) vs {state['black_name']} (magenta) — "
            f"{len(state.get('moves', []))} half-moves. gg!")
    _publish(gameid, parent_id, state["white"], state["black"], body, png)
    _save_game(gameid, state)


# ---- new game ----------------------------------------------------------------
def _start_game(note, own_pk):
    sender = (note.get("user") or {}).get("pubkey")
    opponents = [p for p in _ptags(note) if p and p != own_pk and p != sender]
    if not opponents:
        _reply_text(note, "♟️ To start a #chesstr game, tag me AND the player you want to challenge, "
                          "e.g. \"@me chess @opponent\". You'll be White.")
        return
    gameid = note["id"]
    if _load_game(gameid):
        return  # already started for this note
    white, black = sender, opponents[0]
    board = chess.Board()
    state = {
        "v": 1, "white": white, "black": black,
        "white_name": _name(white), "black_name": _name(black),
        "fen": board.fen(), "moves": [], "last_move": None,
        "status": "active", "root": gameid, "started": int(time.time()),
        "last_board_event": None,
    }
    print(f"[chesstr] new game {gameid[:12]} {state['white_name']} vs {state['black_name']}", flush=True)
    _post_active_board(state, gameid, gameid, san=None)


# ---- a move in an existing game ----------------------------------------------
def _handle_move(note, gameid, state):
    sender = (note.get("user") or {}).get("pubkey")
    if state.get("status") != "active":
        return
    if sender not in (state["white"], state["black"]):
        return  # a spectator chiming in — ignore
    board = chess.Board(state["fen"])
    side_pk = state["white"] if board.turn == chess.WHITE else state["black"]
    if sender != side_pk:
        _reply_text(note, "⏳ It's not your turn.")
        return
    text = _clean_text(note)
    if text.lower() in ("resign", "i resign", "gg", "/resign"):
        winner = state["black_name"] if sender == state["white"] else state["white_name"]
        state["status"] = "resigned"
        _post_gameover(state, gameid, note["id"], None, f"{_name(sender)} resigned. {winner} wins!")
        return
    mv = _parse_move(board, text)
    if mv == "no_piece":
        _reply_text(note, "🤔 I don't see a piece with that number. Use the numbers shown on YOUR pieces.")
        return
    if mv == "illegal" or mv is None or mv not in board.legal_moves:
        # Show the legal destinations for the numbered piece if we can, else generic help.
        hint = ""
        m = _MOVE_RE.search(text)
        if m:
            nums = chess_render.piece_numbers(board, board.turn)
            fs = nums.get(int(m.group(1)))
            if fs is not None:
                dests = sorted(chess.square_name(x.to_square) for x in board.legal_moves if x.from_square == fs)
                hint = (" That piece can go to: " + ", ".join(dests) + ".") if dests else " That piece has no legal moves."
        _reply_text(note, f"🚫 Illegal move.{hint} Try '<number> <square>', e.g. '1 d4'.")
        return
    san = board.san(mv)
    board.push(mv)
    state["fen"] = board.fen()
    state["moves"].append(san)
    state["last_move"] = [mv.from_square, mv.to_square]
    status, result = _status_for(board)
    if status != "active":
        state["status"] = status
        print(f"[chesstr] game {gameid[:12]} over: {result}", flush=True)
        _post_gameover(state, gameid, note["id"], san, result)
    else:
        _post_active_board(state, gameid, note["id"], san)


# ---- main poll ---------------------------------------------------------------
def process_chess():
    own = _nk.get_own_account()
    if not own:
        print("[chesstr] no account (NOSTR_NSEC missing) — idle", flush=True)
        return
    own_pk = own.get("pubkey")
    cutoff = int(time.time()) - _POLL_LOOKBACK_DAYS * 86400
    for note in _nk.get_mentions(limit=40):
        nid = note.get("id")
        if not nid:
            continue
        if (note.get("user") or {}).get("pubkey") == own_pk:
            continue
        ev = note.get("_event") or {}
        if ev.get("created_at", 0) < cutoff:
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
            print(f"[chesstr] processing {nid[:12]} failed: {e}", flush=True)
            import traceback
            traceback.print_exc()
