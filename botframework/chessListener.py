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
dedicated kind-30388 event keyed by the game's root note id, so games survive restarts and never
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

_KIND_APP = 30388
# Must name THIS game — a bare "start" used to match, but every game listener shares the one bot
# identity, so "start #hangman" fired chess too. The app always posts the #chess tag.
_START_RE = re.compile(r"\b(?:chess(?:tr)?)\b", re.IGNORECASE)
# An app-embedded game pointer inside a DM ("g:<64-hex-root> <move>"); bare human DM replies omit it
# and fall back to the per-player pending-game pointer.
_DM_GAME_RE = re.compile(r"\bg:([0-9a-f]{64})\b", re.IGNORECASE)
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
_DM_IDS_FILE = os.path.join(script_dir, f".processed_chess_dms_{_suffix()}")
_MAX_IDS = 5000


def _claim_in(ids_file: str, item_id: str) -> bool:
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
                fd, tmp = tempfile.mkstemp(dir=script_dir, prefix=".chids_")
                with os.fdopen(fd, "w") as f:
                    f.write("\n".join(ids))
                os.replace(tmp, ids_file)
                return True
            finally:
                fcntl.flock(lk.fileno(), fcntl.LOCK_UN)
    except Exception as e:
        print(f"[chesstr] claim failed: {e}", flush=True)
        return False


def _claim_dm(rumor_id: str) -> bool:
    return _claim_in(_DM_IDS_FILE, rumor_id)


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


# ---- game state store (replaceable kind-30388, keyed by game root id) --------
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


def _clean_dm_text(text: str) -> tuple:
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


def _publish(gameid, parent_id, white, black, body, png, federate=True):
    """Post a board image as a reply (root=gameid), tagging both players + #chesstr. `federate=False`
    keeps it local-only (the relay won't re-broadcast it upstream) — used for mid-game move boards so
    only the opening + final posts are public to the wider network (anti-spam)."""
    info = _nk._run(_nk._svc.media.upload(_nk._MEDIA_CFG, _nk._SECKEY, png, "image/png")) or {}
    url = info.get("url")
    if not url:
        raise RuntimeError("board image upload failed")
    # The invite + #chesstr go in the post TEXT (below the image), not on the board image itself.
    # Real mentions, not bare @handles: a p-tag notifies but renders as plain text, so a result
    # post read "@npub1mq3s439… wins" — unrendered AND truncated. See _nk.mentionify.
    content = _nk.mentionify(f"{body}\n{url}\n\n{_footer()}", [white, black], _name)
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
# Iterative-deepening negamax bounded by a WALL-CLOCK budget (like connect4): deepen 1,2,…_MAX_DEPTH
# until the budget is spent, then play the best move from the last fully-searched depth — so a move
# can never peg a core regardless of position / concurrent games. _MAX_DEPTH is just a ceiling now.
_MAX_DEPTH = max(1, int(os.getenv("CHESS_BOT_DEPTH", "3")))
_MOVE_BUDGET_S = max(0.05, float(os.getenv("CHESS_MOVE_BUDGET_S", "0.5")))


class _SearchTimeout(Exception):
    """Raised deep in the search to abort the current (unfinished) depth when the budget is spent."""


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


def _negamax(board: chess.Board, depth: int, alpha: int, beta: int, deadline: float, nodes: list) -> int:
    nodes[0] += 1
    if (nodes[0] & 1023) == 0 and time.monotonic() > deadline:   # cheap periodic time check
        raise _SearchTimeout
    if depth == 0 or board.is_game_over():
        return _evaluate(board)
    best = -10_000_000
    for mv in board.legal_moves:
        board.push(mv)
        val = -_negamax(board, depth - 1, -beta, -alpha, deadline, nodes)
        board.pop()
        if val > best:
            best = val
        if best > alpha:
            alpha = best
        if alpha >= beta:
            break
    return best


def _bot_choose_move(board: chess.Board):
    """Pick the bot's move via iterative-deepening negamax bounded by _MOVE_BUDGET_S. Keeps the best
    set from the last FULLY-searched depth (small random tie-break among near-best so games aren't
    identical), aborting mid-search when out of time. Returns a chess.Move or None."""
    legal = list(board.legal_moves)
    if not legal:
        return None
    deadline = time.monotonic() + _MOVE_BUDGET_S
    nodes = [0]
    base = len(board.move_stack)
    near = legal[:]   # fallback before any depth completes: any legal move
    for d in range(1, _MAX_DEPTH + 1):           # iterative deepening
        scored, best_val = [], -10_000_000
        try:
            for mv in legal:
                board.push(mv)
                val = -_negamax(board, d - 1, -10_000_000, 10_000_000, deadline, nodes)
                board.pop()
                scored.append((val, mv))
                if val > best_val:
                    best_val = val
        except _SearchTimeout:
            while len(board.move_stack) > base:   # unwind any moves left pushed by the aborted search
                board.pop()
            break
        near = [mv for val, mv in scored if val >= best_val - 15]   # completed depth d → adopt its result
        if best_val >= 1_000_000 or time.monotonic() > deadline:    # forced mate found, or out of time
            break
    return near[int.from_bytes(os.urandom(2), "big") % len(near)]


def _apply_bot_moves(state) -> str | None:
    """While it's the bot's turn (its pubkey to move) and the game's active, make the bot's move(s).
    Mutates `state` (fen/moves/last_move/status). Returns the bot's last SAN for display."""
    last_san = None
    # A self-game (white == black) has no human to hand back to, so this loop would play BOTH sides
    # of a whole game in one call — many time-budgeted searches back-to-back → a pegged core. Never
    # auto-play a self-game (creation is also blocked in _start_game; this guards legacy/edge ones).
    if state.get("white") == state.get("black"):
        return None
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
    return ("♟️ Wanna start your own game? Mention me with \"start @friend\" to challenge them "
            "(or just \"start\" to play me); I'll DM each of you the board to make your moves." + play
            + "\n#chess #nostr #gamestr")


def _status_for(board: chess.Board):
    out = board.outcome(claim_draw=True)
    if out is None:
        return "active", ""
    if out.winner is not None:
        return "checkmate", ("White wins by checkmate!" if out.winner == chess.WHITE else "Black wins by checkmate!")
    return "draw", f"½–½ {_DRAW_LABELS.get(out.termination, 'Draw')}."


def _dm_current_player(state, gameid):
    """DM the board to the side now to move (private gameplay). No-op if it's the bot's turn.
    Sets the per-player pending-game pointer so a bare DM reply (no app marker) routes back here."""
    board = chess.Board(state["fen"])
    mover_white = board.turn == chess.WHITE
    mover_pk = state["white"] if mover_white else state["black"]
    if not mover_pk or mover_pk == _nk._PUBKEY:
        return
    opp_nm = state["black_name"] if mover_white else state["white_name"]
    chk = " — you're in CHECK!" if board.is_check() else ""
    title = f"YOUR MOVE{(' — CHECK!' if board.is_check() else '')}"
    sub = f"{state['white_name']} (cyan) vs {state['black_name']} (magenta)  ·  move {board.fullmove_number}"
    png = chess_render.render_board(state["fen"], last_move=state.get("last_move"),
                                    number_color=board.turn, title=title, subtitle=sub, footer="")
    try:
        info = _nk._run(_nk._svc.media.upload(_nk._MEDIA_CFG, _nk._SECKEY, png, "image/png")) or {}
        url = info.get("url") or ""
    except Exception as e:
        print(f"[chesstr] DM board upload failed: {e}", flush=True)
        url = ""
    last = f"Last move: {state['moves'][-1]}. " if state.get("moves") else ""
    body = (f"♟️ Your move vs {opp_nm}{chk}\n{last}"
            f"You're {'White (cyan)' if mover_white else 'Black (magenta)'}.\n"
            + (url + "\n\n" if url else "")
            + "Reply to this DM with your move — your piece's number + square, e.g. '1 d4' "
              "(or 'Nf3' / 'e4' / 'O-O' / 'resign'). Or play from the Chess tab in the app.")
    try:
        _nk.send_dm(mover_pk, body, extra_tags=[["g", gameid]])
        _set_player_game(mover_pk, gameid)
    except Exception as e:
        print(f"[chesstr] send_dm failed: {e}", flush=True)


def _post_active_board(state, gameid, parent_id, san):
    """After a move (or at game start) advance the game. Gameplay is PRIVATE: the board goes to the
    side-to-move as a DM. Only the OPENING invitation (san is None) is posted publicly (to gather
    interest); the FINAL result is posted by _post_gameover. Mid-game = DM only, no public post."""
    if san is not None:
        # MID-GAME: no public post — just persist + DM the next player their board.
        _save_game(gameid, state)
        _dm_current_player(state, gameid)
        return
    # OPENING: one public invitation post, then DM the first player their board.
    board = chess.Board(state["fen"])
    mover_white = board.turn == chess.WHITE
    mover_nm = state["white_name"] if mover_white else state["black_name"]
    move_no = board.fullmove_number
    chk = " — CHECK!" if board.is_check() else ""
    title = f"{'WHITE' if mover_white else 'BLACK'} to move{chk}"
    sub = f"{state['white_name']} (cyan) vs {state['black_name']} (magenta)  ·  move {move_no}"
    png = chess_render.render_board(state["fen"], last_move=state.get("last_move"),
                                    number_color=board.turn, title=title, subtitle=sub, footer=_footer())
    vs_bot = _nk._PUBKEY in (state["white"], state["black"])
    if vs_bot:
        human_white = state["white"] != _nk._PUBKEY
        human_nm = state["white_name"] if human_white else state["black_name"]
        body = (f"🤖 #chess — {human_nm} vs the bot!\n"
                f"You're {'White' if human_white else 'Black'}. 📩 Check your DMs — I've sent you the "
                f"board there, and the whole game plays out privately in DMs. The result gets posted here.")
    else:
        body = (f"♟️ #chess — {state['white_name']} (cyan) has been challenged by "
                f"{state['black_name']} (magenta)!\n"
                f"📩 {mover_nm}, you're White — check your DMs to make the first move. The game plays out "
                f"privately in DMs; I'll post the result here when it's over.")
    ev = _publish(gameid, parent_id, state["white"], state["black"], body, png, federate=True)
    state["last_board_event"] = ev.get("id")
    _save_game(gameid, state)
    _dm_current_player(state, gameid)


def _post_gameover(state, gameid, parent_id, san, result_text, winner_pk="__auto__"):
    board = chess.Board(state["fen"])
    # Persist the outcome on the game STATE so every client (web cards + external) can show clearly
    # WHO WON, not just "game over". winner_pk: a player pubkey, or None for a draw. "__auto__" =
    # derive from a checkmate position (the side NOT to move delivered mate).
    if winner_pk == "__auto__":
        if board.is_checkmate():
            winner_pk = state["white"] if board.turn == chess.BLACK else state["black"]
        else:
            winner_pk = None
    state["result"] = result_text
    state["winner_pk"] = winner_pk
    state["winner_name"] = (state["white_name"] if winner_pk == state["white"]
                            else state["black_name"] if winner_pk == state["black"] else None)
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
    # Never create a self-game (white == black) — it has no human to alternate with, so the bot
    # would auto-play BOTH sides and peg a core. (Belt-and-braces with the _apply_bot_moves guard.)
    if not white or white == black:
        print(f"[chesstr] skip self/invalid game (white==black) for {gameid[:12]}", flush=True)
        return
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


# ---- a move in an existing game (shared by the public-reply + private-DM paths) --------------
def _apply_move(sender, gameid, state, text, reply, parent_id):
    """Apply one move from `sender`. `reply(msg)` sends a nudge/error on the same channel (public
    reply or DM). Game-over posts go to `parent_id` (public). Mid-game = no public post; the next
    player is DM'd by _post_active_board."""
    if sender not in (state["white"], state["black"]):
        return
    if state.get("status") != "active":
        reply("🏁 This game is already over. Start a new one with \"start @opponent\".")
        return
    board = chess.Board(state["fen"])
    if text.lower() in ("resign", "i resign", "gg", "/resign", "quit", "abandon"):
        winner_pk = state["black"] if sender == state["white"] else state["white"]
        winner = state["black_name"] if sender == state["white"] else state["white_name"]
        state["status"] = "resigned"
        _post_gameover(state, gameid, parent_id, None, f"{_name(sender)} resigned. {winner} wins!", winner_pk=winner_pk)
        return
    side_pk = state["white"] if board.turn == chess.WHITE else state["black"]
    if sender != side_pk:
        reply("⏳ It's not your turn.")
        return
    mv = _parse_move(board, text)
    if mv == "no_piece":
        reply("🤔 I don't see a piece with that number. Use the numbers shown on YOUR pieces.")
        return
    if mv == "illegal" or mv is None or mv not in board.legal_moves:
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
        reply(f"🚫 Illegal move.{hint} Reply with '<number> <square>' (e.g. '1 d4'), or SAN like 'Nf3'.")
        return
    san = board.san(mv)
    board.push(mv)
    state["fen"] = board.fen()
    state["moves"].append(san)
    state["last_move"] = [mv.from_square, mv.to_square]
    status, result = _status_for(board)
    if status != "active":
        state["status"] = status
        _post_gameover(state, gameid, parent_id, san, result)
        return
    next_pk = state["white"] if board.turn == chess.WHITE else state["black"]
    if next_pk == _nk._PUBKEY:
        bot_san = _apply_bot_moves(state) or san
        if state.get("status") != "active":
            _, result = _status_for(chess.Board(state["fen"]))
            _post_gameover(state, gameid, parent_id, bot_san, result)
        else:
            _post_active_board(state, gameid, parent_id, bot_san)
    else:
        _post_active_board(state, gameid, parent_id, san)


def _handle_move(note, gameid, state):
    """Public-reply move path (cross-client public play still works)."""
    sender = (note.get("user") or {}).get("pubkey")
    _apply_move(sender, gameid, state, _clean_text(note), lambda m: _reply_text(note, m), note["id"])


def _handle_dm(sender, gameid, state, move_text):
    """Private-DM move path — nudges/errors go back as DMs."""
    _apply_move(sender, gameid, state, move_text,
                lambda m: _nk.send_dm(sender, m), state.get("last_board_event") or gameid)


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
    # ---- private gameplay: read move DMs (NIP-17) ----------------------------
    try:
        dms = _nk.read_dms(limit=100)
    except Exception as e:
        print(f"[chesstr] read_dms failed: {e}", flush=True)
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
            print(f"[chesstr] DM move {rid[:12]} failed: {e}", flush=True)
            import traceback
            traceback.print_exc()
