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
# Anti-spam: cap how many NEW games a single pubkey can start per hour.
_INVITE_MAX = int(os.getenv("CHESS_INVITE_MAX_PER_HOUR", "3"))
_INVITE_WINDOW = 3600
_invite_times: dict = {}   # pubkey -> [unix ts] (per-process; resets on restart)


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
    return _load_doc(_dtag(gameid))


def _load_doc(dtag: str):
    try:
        evs = _nk._run(_nk._svc.relay.query(
            _nk._RELAYS, [{"authors": [_nk._PUBKEY], "kinds": [_KIND_APP],
                           "#d": [dtag], "limit": 1}])) or []
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
def _player_dtag(pk: str) -> str:
    return f"pcai:chesstr:player:{pk}"


def _get_player_game(pk: str):
    doc = _load_doc(_player_dtag(pk))
    return doc.get("gameid") if isinstance(doc, dict) else None


def _set_player_game(pk: str, gameid: str):
    ev = _ev.build_event(_nk._SECKEY, _KIND_APP, json.dumps({"gameid": gameid}, separators=(",", ":")),
                         tags=[["d", _player_dtag(pk)]])
    _nk._run(_nk._svc.relay.publish(_nk._RELAYS, ev))


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
    t = re.sub(r"@[\w@.]+", "", t)
    t = re.sub(r"#\w+", "", t)            # strip hashtags (#chesstr etc.) — they're not the move
    # the move is on the first non-empty line (clients append "\n\n#chesstr" after it)
    for line in t.splitlines():
        if line.strip():
            return line.strip()
    return t.strip()


def _publish(gameid, parent_id, white, black, body, png, federate=True):
    """Post a board image as a reply (root=gameid), tagging both players + #chesstr. `federate=False`
    keeps it local-only (the relay won't re-broadcast it upstream) — used for mid-game move boards so
    only the opening + final posts are public to the wider network (anti-spam)."""
    info = _nk._run(_nk._svc.media.upload(_nk._MEDIA_CFG, _nk._SECKEY, png, "image/png")) or {}
    url = info.get("url")
    if not url:
        raise RuntimeError("board image upload failed")
    # The invite + #chesstr go in the post TEXT (below the image), not on the board image itself.
    content = f"{body}\n{url}\n\n{_footer()}"
    tags = [["e", gameid, "", "root"]]
    if parent_id and parent_id != gameid:
        tags.append(["e", parent_id, "", "reply"])
    for pk in (white, black):
        if pk:
            tags.append(["p", pk])
    for _t in ("chess", "nostr", "gamestr"):
        tags.append(["t", _t])
    if not federate:
        tags.append(["nofederate", "1"])
    tags.append(_ev.imeta_tag(url, "image/png", info.get("sha256", ""), info.get("dim", "")))
    ev = _ev.build_event(_nk._SECKEY, 1, content, tags=tags)
    _nk._run(_nk._svc.relay.publish(_nk._RELAYS, ev))
    return ev


def _reply_text(parent_note, text):
    """A plain text reply (errors / nudges) — also #chesstr-tagged, in the game thread."""
    try:
        _nk.send_reply(parent_note, text + "\n\n#chess #nostr #gamestr")
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
    # UCI FIRST ("e2e4", "g1f3", "e7e8q") — the app's tap-to-move sends this. Must come before the
    # numbered regex, which would otherwise match "2e4" inside "e2e4" and read it as piece #2 → e4.
    if re.fullmatch(r"[a-h][1-8][a-h][1-8][qrbnQRBN]?", low):
        try:
            mv = chess.Move.from_uci(low)
            if mv in board.legal_moves:
                return mv
            # auto-queen if a promotion UCI came without the suffix
            promo = chess.Move(mv.from_square, mv.to_square, promotion=chess.QUEEN)
            if promo in board.legal_moves:
                return promo
            return "illegal"
        except Exception:
            return "illegal"
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


# ---- built-in opponent (play the bot directly) ------------------------------
_PIECE_VAL = {chess.PAWN: 100, chess.KNIGHT: 320, chess.BISHOP: 330,
              chess.ROOK: 500, chess.QUEEN: 900, chess.KING: 0}
_BOT_DEPTH = max(1, int(os.getenv("CHESS_BOT_DEPTH", "2")))


def _evaluate(board: chess.Board) -> int:
    """Material balance from the side-to-move's perspective (+ tiny mobility), for negamax."""
    if board.is_checkmate():
        return -1_000_000
    if board.is_stalemate() or board.is_insufficient_material():
        return 0
    score = 0
    for pt, val in _PIECE_VAL.items():
        score += val * (len(board.pieces(pt, chess.WHITE)) - len(board.pieces(pt, chess.BLACK)))
    score += 3 * (sum(1 for _ in board.legal_moves) if board.turn == chess.WHITE else 0)
    return score if board.turn == chess.WHITE else -score


def _negamax(board: chess.Board, depth: int, alpha: int, beta: int) -> int:
    if depth == 0 or board.is_game_over():
        return _evaluate(board)
    best = -10_000_000
    for mv in board.legal_moves:
        board.push(mv)
        val = -_negamax(board, depth - 1, -beta, -alpha)
        board.pop()
        if val > best:
            best = val
        if best > alpha:
            alpha = best
        if alpha >= beta:
            break
    return best


def _bot_choose_move(board: chess.Board):
    """Pick the bot's move (negamax, material eval). Small random tie-break among near-best moves so
    games aren't identical. Returns a chess.Move or None."""
    best_val, scored = -10_000_000, []
    for mv in board.legal_moves:
        board.push(mv)
        val = -_negamax(board, _BOT_DEPTH - 1, -10_000_000, 10_000_000)
        board.pop()
        scored.append((val, mv))
        if val > best_val:
            best_val = val
    near = [mv for val, mv in scored if val >= best_val - 15]
    if not near:
        return None
    return near[int.from_bytes(os.urandom(2), "big") % len(near)]


def _apply_bot_moves(state) -> str | None:
    """While it's the bot's turn (its pubkey to move) and the game's active, make the bot's move(s).
    Mutates `state` (fen/moves/last_move/status). Returns the bot's last SAN for display."""
    last_san = None
    board = chess.Board(state["fen"])
    while state.get("status") == "active":
        side_pk = state["white"] if board.turn == chess.WHITE else state["black"]
        if side_pk != _nk._PUBKEY:
            break
        mv = _bot_choose_move(board)
        if not mv:
            break
        last_san = board.san(mv)
        board.push(mv)
        state["fen"] = board.fen()
        state["moves"].append(last_san)
        state["last_move"] = [mv.from_square, mv.to_square]
        status, _ = _status_for(board)
        if status != "active":
            state["status"] = status
            break
    return last_san


def _footer() -> str:
    """Board-image footer: invite people to play interactively in the app, then the #chesstr tag."""
    site = (os.getenv("CHESS_SITE_URL", "") or "").strip()
    play = f"\nPlay interactively at {site}." if site else ""
    return ("♟️ Wanna start your own game with a friend? Reply \"chess @friend\" to challenge them "
            "(or just \"chess\" to play me); then reply with moves like \"1 d4\"." + play + "\n#chess #nostr #gamestr")


def _status_for(board: chess.Board):
    out = board.outcome(claim_draw=True)
    if out is None:
        return "active", ""
    if out.winner is not None:
        return "checkmate", ("White wins by checkmate!" if out.winner == chess.WHITE else "Black wins by checkmate!")
    return "draw", f"½–½ {_DRAW_LABELS.get(out.termination, 'Draw')}."


def _post_active_board(state, gameid, parent_id, san):
    """Render + post the board for the side now to move (after a move or at game start)."""
    # MID-GAME: state-only, NO public post — the web client renders the board from this state and
    # polls it. Only the opening (san is None) and the final (_post_gameover) are posted publicly.
    if san is not None:
        _save_game(gameid, state)
        return
    board = chess.Board(state["fen"])
    mover_white = board.turn == chess.WHITE
    mover_pk = state["white"] if mover_white else state["black"]
    mover_nm = state["white_name"] if mover_white else state["black_name"]
    move_no = board.fullmove_number
    chk = " — CHECK!" if board.is_check() else ""
    title = f"{'WHITE' if mover_white else 'BLACK'} to move{chk}"
    sub = f"{state['white_name']} (cyan) vs {state['black_name']} (magenta)  ·  move {move_no}"
    png = chess_render.render_board(state["fen"], last_move=state.get("last_move"),
                                    number_color=board.turn, title=title, subtitle=sub, footer=_footer())
    if san is None:
        # Opening post = the invitation. White moves first, so the challenged player "accepts" by moving.
        vs_bot = _nk._PUBKEY in (state["white"], state["black"])
        if vs_bot:
            human_white = state["white"] != _nk._PUBKEY
            human_nm = state["white_name"] if human_white else state["black_name"]
            body = (f"🤖 #chesstr — {human_nm} vs the bot!\n"
                    f"You're {'White' if human_white else 'Black'}. Make your move: reply to THIS post "
                    f"with your piece's number + square, e.g. '1 d4' (or 'Nf3' / 'e4' / 'O-O'). "
                    f"Your pieces are numbered on the board above, or play from the Chess tab in the app.")
        else:
            body = (f"♟️ #chesstr — {state['white_name']} (cyan) has been challenged by "
                    f"{state['black_name']} (magenta)!\n"
                    f"{mover_nm}, you're up first (White) — ACCEPT by making your move. "
                    f"Reply to THIS post with your piece's number + square, e.g. '1 d4' "
                    f"(or 'Nf3' / 'e4' / 'O-O'). Your pieces are numbered on the board above. "
                    f"You can also play from the Chess tab in the app.")
    else:
        last = f"Last move: {san}. "
        body = (f"♟️ {last}{mover_nm} ({'cyan' if mover_white else 'magenta'}) to move{chk}\n"
                f"Reply to THIS post with your move, e.g. '1 d4' (move piece #1 to d4). "
                f"SAN/UCI/O-O also work.")
    # Opening (san is None) is public; mid-game move boards stay local-only (anti-spam).
    ev = _publish(gameid, parent_id, state["white"], state["black"], body, png, federate=(san is None))
    state["last_board_event"] = ev.get("id")
    _save_game(gameid, state)


def _post_gameover(state, gameid, parent_id, san, result_text):
    board = chess.Board(state["fen"])
    png = chess_render.render_board(state["fen"], last_move=state.get("last_move"),
                                    number_color=None, title="GAME OVER", subtitle=result_text, footer=_footer())
    body = (f"🏁 {('Last move: ' + san + '. ') if san else ''}{result_text}\n"
            f"{state['white_name']} (cyan) vs {state['black_name']} (magenta) — "
            f"{len(state.get('moves', []))} half-moves. gg!")
    _publish(gameid, parent_id, state["white"], state["black"], body, png)
    _save_game(gameid, state)


# ---- new game ----------------------------------------------------------------
def _start_game(note, own_pk):
    sender = (note.get("user") or {}).get("pubkey")
    opponents = [p for p in _ptags(note) if p and p != own_pk and p != sender]
    gameid = note["id"]
    if _load_game(gameid):
        return  # already started for this note
    # Anti-spam: cap new games per starter per hour.
    now = time.time()
    recent = [t for t in _invite_times.get(sender, []) if now - t < _INVITE_WINDOW]
    if _INVITE_MAX and len(recent) >= _INVITE_MAX:
        _reply_text(note, f"⏳ You've started {_INVITE_MAX} games in the last hour — that's the limit. "
                          "Finish or wait a bit before starting another.")
        return
    recent.append(now)
    _invite_times[sender] = recent
    # No human opponent tagged → play the BOT itself (human is White, the bot is Black).
    vs_bot = not opponents
    white, black = sender, (opponents[0] if opponents else own_pk)
    # Invite flow (human vs human): a ["chess_first", <pubkey>] tag makes that player White.
    first = next((t[1] for t in _tags(note) if len(t) >= 2 and t[0] == "chess_first" and t[1]), None)
    if opponents and first in (sender, opponents[0]):
        white = first
        black = opponents[0] if first == sender else sender
    # No limit on concurrent active games — a player can have several going at once (e.g. vs the bot
    # AND vs humans). Abuse is bounded by the per-hour INVITE rate limit above, not by abandoning
    # games. (Each game is independent, keyed by its own root id.)
    board = chess.Board()
    state = {
        "v": 1, "white": white, "black": black,
        "white_name": _name(white), "black_name": _name(black),
        "fen": board.fen(), "moves": [], "last_move": None,
        "status": "active", "root": gameid, "started": int(time.time()),
        "last_board_event": None,
    }
    print(f"[chesstr] new game {gameid[:12]} {state['white_name']} vs {state['black_name']}"
          f"{' (vs bot)' if vs_bot else ''}", flush=True)
    # If the bot is to move first (it's White), play its move into the opening.
    if state["white"] == own_pk:
        _apply_bot_moves(state)
    _post_active_board(state, gameid, gameid, san=None)


# ---- a move in an existing game ----------------------------------------------
def _handle_move(note, gameid, state):
    sender = (note.get("user") or {}).get("pubkey")
    if sender not in (state["white"], state["black"]):
        return  # a spectator chiming in — ignore
    if state.get("status") != "active":
        if state.get("status") == "abandoned":
            _reply_text(note, "⚠️ This game was abandoned (a newer #chesstr game superseded it). "
                              "Start a fresh one with \"chess @opponent\".")
        else:
            _reply_text(note, "🏁 This game is already over. Start a new one with \"chess @opponent\".")
        return
    board = chess.Board(state["fen"])
    text = _clean_text(note)
    # Resign/quit is allowed at ANY time (even on the opponent's turn) — checked before the turn gate.
    if text.lower() in ("resign", "i resign", "gg", "/resign", "quit", "abandon"):
        winner = state["black_name"] if sender == state["white"] else state["white_name"]
        state["status"] = "resigned"
        _post_gameover(state, gameid, note["id"], None, f"{_name(sender)} resigned. {winner} wins!")
        return
    side_pk = state["white"] if board.turn == chess.WHITE else state["black"]
    if sender != side_pk:
        _reply_text(note, "⏳ It's not your turn.")
        return
    mv = _parse_move(board, text)
    print(f"[chesstr] move from {sender[:8]} in {gameid[:8]}: text={text!r} fen={state['fen']!r} -> {mv}", flush=True)
    if mv == "no_piece":
        _reply_text(note, "🤔 I don't see a piece with that number. Use the numbers shown on YOUR pieces.")
        return
    if mv == "illegal" or mv is None or mv not in board.legal_moves:
        # Work out which piece the player meant (numbered OR UCI/tap) so we can explain WHY it's
        # illegal — e.g. "that piece is pinned" — instead of a bare "illegal move".
        fs = None
        m = _MOVE_RE.search(text)
        low = text.strip().lower()
        if m:
            fs = chess_render.piece_numbers(board, board.turn).get(int(m.group(1)))
        elif re.fullmatch(r"[a-h][1-8][a-h][1-8][qrbnQRBN]?", low):
            fs = chess.parse_square(low[:2])
        hint = ""
        if fs is not None:
            pc = board.piece_at(fs)
            if pc is None or pc.color != board.turn:
                hint = " There's no piece of yours there."
            else:
                dests = sorted(chess.square_name(x.to_square) for x in board.legal_moves if x.from_square == fs)
                if dests:
                    hint = f" That {chess.piece_name(pc.piece_type)} can go to: " + ", ".join(dests) + "."
                elif board.is_pinned(board.turn, fs):
                    hint = f" That {chess.piece_name(pc.piece_type)} is PINNED to your king — it can't move right now."
                else:
                    hint = f" That {chess.piece_name(pc.piece_type)} has no legal moves right now."
        _reply_text(note, f"🚫 Illegal move.{hint} Move with '<number> <square>' (e.g. '1 d4'), tap on the Chess tab, or SAN like 'Nf3'.")
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
        return
    # Playing the bot? It replies right away.
    next_pk = state["white"] if board.turn == chess.WHITE else state["black"]
    if next_pk == _nk._PUBKEY:
        bot_san = _apply_bot_moves(state) or san
        if state.get("status") != "active":
            _, result = _status_for(chess.Board(state["fen"]))
            print(f"[chesstr] game {gameid[:12]} over (bot): {result}", flush=True)
            _post_gameover(state, gameid, note["id"], bot_san, result)
        else:
            _post_active_board(state, gameid, note["id"], bot_san)
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
