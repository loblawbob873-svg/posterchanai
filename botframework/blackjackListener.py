"""#blackjack — play Blackjack (21) against the bot dealer over Nostr.

You vs the house: START by posting "blackjack" (or "start") mentioning the bot. The bot deals two
cards each (its hole card hidden), DMs you your hand, and you reply "hit" or "stand". On stand the
dealer draws to 17 and the result is posted publicly. Single-player only (it's you vs the dealer —
no human opponent), so there's no AI search here: the dealer follows a fixed rule, O(1) per action.
State is a replaceable kind-30078 doc keyed by the game root id. Every post carries #blackjack.
"""
import os
import re
import sys
import json
import time
import fcntl
import random
import hashlib
import tempfile

script_dir = os.path.dirname(os.path.abspath(__file__))
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)

import blackjack_render
import nostr as _nk
from config import NOSTR_NSEC
from app.services.nostr import event as _ev

_KIND_APP = 30078
_START_RE = re.compile(r"\b(blackjack|black\s*jack|start)\b", re.IGNORECASE)
_DM_GAME_RE = re.compile(r"\bg:([0-9a-f]{64})\b", re.IGNORECASE)
_NOSTR_TOKEN_RE = re.compile(
    r"nostr:[a-z0-9]+|\b(?:npub1|nprofile1|nevent1|note1|naddr1)[023456789acdefghjklmnpqrstuvwxyz]+",
    re.IGNORECASE)
_LOOKBACK_DAYS = int(os.getenv("BLACKJACK_LOOKBACK_DAYS", "3"))
_INVITE_MAX = int(os.getenv("BLACKJACK_INVITE_MAX_PER_HOUR", "6"))
_INVITE_WINDOW = 3600
_invite_times: dict = {}


# ---- cross-restart dedup --------------------------------------------------
def _suffix():
    return hashlib.sha1((NOSTR_NSEC or "").encode()).hexdigest()[:10] if NOSTR_NSEC else "default"


_IDS_FILE = os.path.join(script_dir, f".processed_blackjack_ids_{_suffix()}")
_DM_IDS_FILE = os.path.join(script_dir, f".processed_blackjack_dms_{_suffix()}")


def _claim_in(ids_file, item_id):
    lock = ids_file + ".lock"
    try:
        with open(lock, "w") as lk:
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
                if len(ids) > 5000:
                    ids = set(sorted(ids)[-5000:])
                fd, tmp = tempfile.mkstemp(dir=script_dir, prefix=".bjids_")
                with os.fdopen(fd, "w") as f:
                    f.write("\n".join(ids))
                os.replace(tmp, ids_file)
                return True
            finally:
                fcntl.flock(lk.fileno(), fcntl.LOCK_UN)
    except Exception as e:
        print(f"[blackjack] claim failed: {e}", flush=True)
        return False


def _claim(note_id):
    return _claim_in(_IDS_FILE, note_id)


def _claim_dm(rumor_id):
    return _claim_in(_DM_IDS_FILE, rumor_id)


# ---- state store (kind-30078) ---------------------------------------------
def _dtag(gameid):
    return f"pcai:blackjack:{gameid}"


def _save_game(gameid, state):
    ev = _ev.build_event(_nk._SECKEY, _KIND_APP, json.dumps(state, separators=(",", ":")),
                         tags=[["d", _dtag(gameid)]])
    _nk._run(_nk._svc.relay.publish(_nk._RELAYS, ev))


def _load_doc(dtag):
    try:
        evs = _nk._run(_nk._svc.relay.query(
            _nk._RELAYS, [{"authors": [_nk._PUBKEY], "kinds": [_KIND_APP], "#d": [dtag], "limit": 1}])) or []
    except Exception:
        return None
    evs.sort(key=lambda e: e.get("created_at", 0), reverse=True)
    try:
        return json.loads(evs[0].get("content") or "{}") if evs else None
    except Exception:
        return None


def _load_game(gameid):
    return _load_doc(_dtag(gameid))


def _player_dtag(pk):
    return f"pcai:blackjack:player:{pk}"


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
    return ("🃏 Wanna play? Mention me with \"blackjack\" to sit down — I'll DM you your hand; "
            "reply \"hit\" or \"stand\"." + play + "\n#blackjack #nostr #gamestr")


def _publish(gameid, parent_id, player, body, png, federate=True):
    info = _nk._run(_nk._svc.media.upload(_nk._MEDIA_CFG, _nk._SECKEY, png, "image/png")) or {}
    url = info.get("url")
    if not url:
        raise RuntimeError("board image upload failed")
    content = f"{body}\n{url}\n\n{_footer()}"
    tags = [["e", gameid, "", "root"]]
    if parent_id and parent_id != gameid:
        tags.append(["e", parent_id, "", "reply"])
    if player:
        tags.append(["p", player])
    for _t in ("blackjack", "nostr", "gamestr"):
        tags.append(["t", _t])
    if not federate:
        tags.append(["nofederate", "1"])
    tags.append(_ev.imeta_tag(url, "image/png", info.get("sha256", ""), info.get("dim", "")))
    ev = _ev.build_event(_nk._SECKEY, 1, content, tags=tags)
    _nk._run(_nk._svc.relay.publish(_nk._RELAYS, ev))
    return ev


def _reply_text(note, text):
    try:
        _nk.send_reply(note, text + "\n\n#blackjack #nostr #gamestr")
    except Exception as e:
        print(f"[blackjack] reply failed: {e}", flush=True)


# ---- card logic (no search — O(1) per action) -----------------------------
_RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "T", "J", "Q", "K"]
_SUITS = ["S", "H", "D", "C"]


def _new_deck():
    deck = [r + s for s in _SUITS for r in _RANKS]
    random.shuffle(deck)
    return deck


def _card_val(rank):
    if rank == "A":
        return 11
    if rank in ("T", "J", "Q", "K"):
        return 10
    return int(rank)


def _hand_value(hand):
    total = sum(_card_val(c[:-1]) for c in hand)
    aces = sum(1 for c in hand if c[0] == "A")
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total


def _is_bj(hand):
    return len(hand) == 2 and _hand_value(hand) == 21


def _dealer_play(state):
    """Dealer reveals + draws until 17+ (stands on all 17s)."""
    while _hand_value(state["dhand"]) < 17 and state["deck"]:
        state["dhand"].append(state["deck"].pop())


# ---- start + play ---------------------------------------------------------
def _start_game(note, own_pk):
    sender = (note.get("user") or {}).get("pubkey")
    gameid = note["id"]
    if not sender or _load_game(gameid):
        return
    now = time.time()
    recent = [t for t in _invite_times.get(sender, []) if now - t < _INVITE_WINDOW]
    if _INVITE_MAX and len(recent) >= _INVITE_MAX:
        _reply_text(note, f"⏳ You've started {_INVITE_MAX} hands in the last hour — that's the limit.")
        return
    recent.append(now)
    _invite_times[sender] = recent
    deck = _new_deck()
    phand = [deck.pop(), deck.pop()]
    dhand = [deck.pop(), deck.pop()]
    state = {
        "v": 1, "player": sender, "player_name": _name(sender), "bot": own_pk,
        "deck": deck, "phand": phand, "dhand": dhand, "status": "player",
        "result": "", "outcome": "", "root": gameid, "started": int(time.time()),
        "last_board_event": None,
    }
    print(f"[blackjack] new hand {gameid[:12]} {state['player_name']}", flush=True)
    if _is_bj(phand) or _is_bj(dhand):     # naturals settle immediately
        _resolve(state, gameid, gameid)
        return
    _post(state, gameid, gameid, opening=True)


def _apply_move(sender, gameid, state, text, reply, parent_id):
    if sender != state.get("player"):
        return
    if state.get("status") != "player":
        reply("🏁 This hand is over. Start a new one with \"blackjack\".")
        return
    low = text.lower().strip()
    if low in ("hit", "h", "draw", "card", "twist"):
        if state["deck"]:
            state["phand"].append(state["deck"].pop())
        if _hand_value(state["phand"]) > 21:
            _resolve(state, gameid, parent_id)        # bust
        else:
            _post(state, gameid, parent_id)           # DM the updated hand
    elif low in ("stand", "s", "stay", "hold", "stick"):
        _dealer_play(state)
        _resolve(state, gameid, parent_id)
    elif low in ("resign", "quit", "fold", "abandon", "surrender"):
        _dealer_play(state)
        _resolve(state, gameid, parent_id, forced_lose=True)
    else:
        reply("🃏 Reply 'hit' to draw a card, or 'stand' to hold.")


def _resolve(state, gameid, parent_id, forced_lose=False):
    pv, dv = _hand_value(state["phand"]), _hand_value(state["dhand"])
    pbj, dbj = _is_bj(state["phand"]), _is_bj(state["dhand"])
    if forced_lose:
        outcome, msg = "lose", "You folded — dealer wins."
    elif pv > 21:
        outcome, msg = "lose", f"💥 Bust at {pv}! Dealer wins."
    elif pbj and not dbj:
        outcome, msg = "blackjack", "🃏 Blackjack! You win!"
    elif dbj and not pbj:
        outcome, msg = "lose", "Dealer has Blackjack. You lose."
    elif dv > 21:
        outcome, msg = "win", f"Dealer busts at {dv} — you win 🎉"
    elif pv > dv:
        outcome, msg = "win", f"You win {pv}–{dv} 🎉"
    elif pv < dv:
        outcome, msg = "lose", f"Dealer wins {dv}–{pv}."
    else:
        outcome, msg = "push", f"Push — tie at {pv}."
    state["status"] = "over"
    state["outcome"] = outcome
    state["result"] = msg
    state["winner_pk"] = state["player"] if outcome in ("win", "blackjack") else None
    state["winner_name"] = state["player_name"] if state["winner_pk"] else None
    _post(state, gameid, parent_id, over=True, result=msg)


def _dm_current_player(state, gameid):
    p = state.get("player")
    if not p or p == _nk._PUBKEY:
        return
    pv = _hand_value(state["phand"])
    upcard = state["dhand"][0] if state.get("dhand") else "?"
    png = blackjack_render.render(state["dhand"], state["phand"], None, pv, hide_hole=True,
                                  title="YOUR HAND", subtitle=f"You have {pv} · dealer shows {upcard}")
    try:
        info = _nk._run(_nk._svc.media.upload(_nk._MEDIA_CFG, _nk._SECKEY, png, "image/png")) or {}
        url = info.get("url") or ""
    except Exception as e:
        print(f"[blackjack] DM board upload failed: {e}", flush=True)
        url = ""
    body = (f"🃏 Your hand: {' '.join(state['phand'])}  (= {pv})\nDealer shows {upcard}.\n"
            + (url + "\n\n" if url else "")
            + "Reply 'hit' to draw, or 'stand' to hold. Or play from the Blackjack tab in the app.")
    try:
        _nk.send_dm(p, body, extra_tags=[["g", gameid]])
        _set_player_game(p, gameid)
    except Exception as e:
        print(f"[blackjack] send_dm failed: {e}", flush=True)


def _post(state, gameid, parent_id, opening=False, over=False, result=""):
    # MID-HAND (a hit that didn't bust): no public post — just persist + DM the player privately.
    if not opening and not over:
        _save_game(gameid, state)
        _dm_current_player(state, gameid)
        return
    pv, dv = _hand_value(state["phand"]), _hand_value(state["dhand"])
    title = "GAME OVER" if over else "BLACKJACK"
    sub = result if over else f"{state['player_name']} vs the dealer"
    png = blackjack_render.render(state["dhand"], state["phand"], dv, pv, hide_hole=(not over),
                                  title=title, subtitle=sub)
    if over:
        body = (f"🏁 {result}\n{state['player_name']} vs the dealer — you {pv}, dealer {dv}. gg!")
    else:
        body = (f"🃏 #blackjack — {state['player_name']} sat down at the table!\n"
                f"📩 Check your DMs — your hand's there. Reply 'hit' or 'stand'. I'll post the result here.")
    ev = _publish(gameid, parent_id, state["player"], body, png, federate=True)
    state["last_board_event"] = ev.get("id")
    _save_game(gameid, state)
    if not over:
        _dm_current_player(state, gameid)


def _handle_move(note, gameid, state):
    sender = (note.get("user") or {}).get("pubkey")
    _apply_move(sender, gameid, state, _clean_text(note), lambda m: _reply_text(note, m), note["id"])


def _handle_dm(sender, gameid, state, move_text):
    _apply_move(sender, gameid, state, move_text,
                lambda m: _nk.send_dm(sender, m), state.get("last_board_event") or gameid)


def process_blackjack():
    own = _nk.get_own_account()
    if not own:
        print("[blackjack] no account (NOSTR_NSEC missing) — idle", flush=True)
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
            print(f"[blackjack] processing {nid[:12]} failed: {e}", flush=True)
            import traceback
            traceback.print_exc()
    # ---- private play: read move DMs (NIP-17) --------------------------------
    try:
        dms = _nk.read_dms(limit=100)
    except Exception as e:
        print(f"[blackjack] read_dms failed: {e}", flush=True)
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
            gameid = _get_player_game(sender)
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
            print(f"[blackjack] DM move {rid[:12]} failed: {e}", flush=True)
            import traceback
            traceback.print_exc()
